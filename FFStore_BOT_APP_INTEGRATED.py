import asyncio
import re
import json
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import secrets
import os
import hmac
import hashlib
import base64
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    CommandHandler, ContextTypes, ApplicationHandlerStop, filters
)
from telethon import TelegramClient
from config import API_ID, API_HASH, PHONE, SOURCE_GROUP_ID, TARGET_GROUP_ID, ADMIN_ID

# Keep your current bot token here. For security, rotate it in BotFather later.
# Reuse the token from your working backup without embedding it in this file.
def load_existing_token():
    backup = Path.home() / "relay_before_purchase.py"
    if not backup.exists():
        raise FileNotFoundError("relay_before_purchase.py backup not found in Termux Home.")
    source = backup.read_text(encoding="utf-8")
    match = re.search(r"BOT_TOKEN\s*=\s*[\"']([^\"']+)[\"']", source)
    if not match:
        raise RuntimeError("BOT_TOKEN was not found in relay_before_purchase.py")
    return match.group(1)

BOT_TOKEN = load_existing_token()

client = TelegramClient("ffstore_session", API_ID, API_HASH)
pending = {}
uid_locks = {}
UID_HISTORY_FILE = "uid_history.json"
ORDERS_FILE = "purchase_orders.json"
REFERRAL_FILE = "referral_data.json"
USERS_FILE = "ffstore_users.json"
WALLETS_FILE = "ffstore_wallets.json"
WALLET_TOPUPS_FILE = "ffstore_wallet_topups.json"
ACTIVE_PLANS_FILE = "active_like_plans.json"
IST = ZoneInfo("Asia/Kolkata")
COMMISSION_RATE = 0.10
MIN_WITHDRAWAL = 50
COOLDOWN_SECONDS = 18 * 60 * 60
# Six fixed-amount UPI QR images are stored beside this bot file.
BOT_DIR = Path(__file__).resolve().parent
PLAN_QR_PATHS = {
    "p6": BOT_DIR / "PLAN_7_DAYS_50.png",
    "p1": BOT_DIR / "BASIC_75.png",
    "p2": BOT_DIR / "STANDARD_100.png",
    "p3": BOT_DIR / "POPULAR_170.png",
    "p4": BOT_DIR / "ADVANCED_280.png",
    "p5": BOT_DIR / "PREMIUM_350.png",
}
QR_PATH = str(BOT_DIR / "FFSTORE_PAYMENT_QR.png")  # personal PhonePe QR for wallet top-ups
DUPLICATE_IMAGE = str(
    Path.home() / "storage/downloads/ChatGPT Image Oct 24, 2026, 02_19_28 PM (1).png"
)

PLANS = {
    "p6": ("⚡ — 7 Days | 1,540+ Likes", 50),
    "p1": ("🥉 BASIC — 10 Days | 2,200+ Likes", 75),
    "p2": ("🥈 STANDARD — 15 Days | 3,300+ Likes", 100),
    "p3": ("🥇 POPULAR — 30 Days | 6,600+ Likes", 170),
    "p4": ("⚡ ADVANCED — 45 Days | 9,900+ Likes", 280),
    "p5": ("💎 PREMIUM — 60 Days | 13,200+ Likes", 350),
}

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default

uid_history = load_json(UID_HISTORY_FILE, {})
orders = load_json(ORDERS_FILE, {})
referral_data = load_json(REFERRAL_FILE, {"referrers": {}, "codes": {}, "commissions": {}, "withdrawals": {}})
registered_users = load_json(USERS_FILE, {})
wallets = load_json(WALLETS_FILE, {})
wallet_topups = load_json(WALLET_TOPUPS_FILE, {})
active_like_plans = load_json(ACTIVE_PLANS_FILE, {})
# Razorpay credentials must be configured as environment variables on the server.
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
RAZORPAY_WEBHOOK_PORT = int(os.getenv("RAZORPAY_WEBHOOK_PORT", "8080"))
wallet_lock = threading.RLock()
telegram_app = None
telegram_loop = None
# Backfill users known from saved orders so existing customers are retained for updates.
for _order in orders.values():
    _uid = _order.get("user_id")
    if _uid is not None:
        registered_users.setdefault(str(_uid), {"user_id": int(_uid), "name": _order.get("name", ""), "username": _order.get("username", "")})
for section in ("referrers", "codes", "commissions", "withdrawals"):
    referral_data.setdefault(section, {})

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

save_json(USERS_FILE, registered_users)

def save_active_like_plans():
    save_json(ACTIVE_PLANS_FILE, active_like_plans)


def plan_duration_days(order):
    key = order.get("plan_key")
    durations = {"p6": 7, "p1": 10, "p2": 15, "p3": 30, "p4": 45, "p5": 60}
    return durations.get(key, 30)


def wallet_balance(user_id):
    return max(0, int(wallets.get(str(user_id), 0)))

def save_wallets():
    save_json(WALLETS_FILE, wallets)

def save_wallet_topups():
    save_json(WALLET_TOPUPS_FILE, wallet_topups)

def create_razorpay_payment_link(topup_id, amount, user_id):
    """Create a Razorpay Payment Link. Amount is whole INR; API amount is paise."""
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        raise RuntimeError("Razorpay keys missing. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET environment variables.")
    payload = {
        "amount": int(amount) * 100,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": str(topup_id),
        "description": f"FF Store Wallet Top-up {topup_id}",
        "customer": {"name": str(wallet_topups[topup_id].get("name", "FF Store User"))[:100]},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {"topup_id": str(topup_id), "telegram_user_id": str(user_id), "purpose": "wallet_topup"},
    }
    data = json.dumps(payload).encode("utf-8")
    auth = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/payment_links", data=data,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Razorpay API error ({exc.code}): {detail}") from exc
    if not result.get("short_url") or not result.get("id"):
        raise RuntimeError("Razorpay response did not include a payment link.")
    return result


def _credit_wallet_from_webhook(payload):
    """Verify paid Payment Link webhook and credit a matching top-up only once."""
    event = payload.get("event")
    if event != "payment_link.paid":
        return (200, "ignored event")
    entity = (((payload.get("payload") or {}).get("payment_link") or {}).get("entity") or {})
    topup_id = str(entity.get("reference_id") or "")
    if not topup_id:
        return (400, "missing reference_id")
    topup = wallet_topups.get(topup_id)
    if not topup:
        return (404, "unknown top-up")
    # Match the gateway's confirmed amount/currency against our saved request.
    if entity.get("status") != "paid" or entity.get("currency") != "INR":
        return (400, "payment not paid in INR")
    expected_paise = int(topup.get("amount", 0)) * 100
    if int(entity.get("amount", -1)) != expected_paise or int(entity.get("amount_paid", -1)) != expected_paise:
        return (400, "amount mismatch")
    with wallet_lock:
        # Idempotency: webhook retries must never credit the same top-up twice.
        if topup.get("status") == "credited":
            return (200, "already credited")
        if topup.get("status") != "pending":
            return (200, "top-up is not pending")
        user_id = int(topup["user_id"])
        wallets[str(user_id)] = wallet_balance(user_id) + int(topup["amount"])
        topup["status"] = "credited"
        topup["credited_at"] = int(time.time())
        topup["razorpay_payment_link_id"] = entity.get("id", "")
        save_wallets()
        save_wallet_topups()
    if telegram_app and telegram_loop:
        message = (
            f"✅ Wallet top-up successful!\nAdded: ₹{topup['amount']}\n"
            f"New balance: ₹{wallet_balance(user_id)}\nReference: {topup_id}"
        )
        try:
            asyncio.run_coroutine_threadsafe(telegram_app.bot.send_message(chat_id=user_id, text=message), telegram_loop)
            asyncio.run_coroutine_threadsafe(telegram_app.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"💳 AUTOMATIC WALLET TOP-UP\nReference: {topup_id}\nUser ID: {user_id}\nAmount: ₹{topup['amount']}\nStatus: Razorpay verified"
            ), telegram_loop)
        except Exception as exc:
            print("Wallet top-up notification scheduling failed:", exc)
    return (200, "wallet credited")


def start_razorpay_webhook_server():
    if not RAZORPAY_WEBHOOK_SECRET:
        print("WARNING: RAZORPAY_WEBHOOK_SECRET missing; automatic wallet credits are disabled.")
        return None
    class WebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.rstrip("/") != "/razorpay/webhook":
                self.send_response(404); self.end_headers(); return
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                self.send_response(400); self.end_headers(); return
            raw = self.rfile.read(length)
            signature = self.headers.get("X-Razorpay-Signature", "")
            expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
            if not signature or not hmac.compare_digest(signature, expected):
                self.send_response(401); self.end_headers(); self.wfile.write(b"invalid signature"); return
            try:
                payload = json.loads(raw.decode("utf-8"))
                status, message = _credit_wallet_from_webhook(payload)
            except Exception as exc:
                print("Razorpay webhook processing error:", exc)
                status, message = 500, "processing error"
            self.send_response(status); self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers(); self.wfile.write(message.encode("utf-8"))
        def log_message(self, fmt, *args):
            print("Razorpay webhook:", fmt % args)
    server = ThreadingHTTPServer(("0.0.0.0", RAZORPAY_WEBHOOK_PORT), WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="razorpay-webhook")
    thread.start()
    print(f"Razorpay webhook listener running on port {RAZORPAY_WEBHOOK_PORT}; expose /razorpay/webhook via HTTPS reverse proxy.")
    return server


def wallet_topup_keyboard(topup_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve & Add Balance", callback_data=f"wallet:approve:{topup_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"wallet:reject:{topup_id}"),
    ]])

def payment_choice_keyboard(order_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🪙 Pay from Wallet", callback_data=f"wallet:pay:{order_id}")],
        [InlineKeyboardButton("💳 Pay by QR", callback_data=f"method:qr:{order_id}")],
    ])

def topup_amount_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("₹30", callback_data="topup:amount:30"), InlineKeyboardButton("₹50", callback_data="topup:amount:50")],
        [InlineKeyboardButton("₹100", callback_data="topup:amount:100"), InlineKeyboardButton("₹200", callback_data="topup:amount:200")],
        [InlineKeyboardButton("₹500", callback_data="topup:amount:500"), InlineKeyboardButton("✏️ Custom Amount", callback_data="topup:custom")],
    ])

def save_referral_data():
    save_json(REFERRAL_FILE, referral_data)

def register_user(user):
    if not user:
        return
    registered_users[str(user.id)] = {
        "user_id": user.id, "name": user.full_name or "", "username": user.username or ""
    }
    save_json(USERS_FILE, registered_users)

def community_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Join FF Store Group (Optional)", url="https://t.me/FFISTORE")],
        [InlineKeyboardButton("📢 Join FF Daily News (Optional)", url="https://t.me/ffdailynews")],
    ])


def main_reply_keyboard():
    """Free Fire themed persistent reply keyboard: two buttons per row."""
    return ReplyKeyboardMarkup(
        [
            ["❤️ RECEIVE 220 LIKE 🔥", "💎 BUY LIKES"],
            ["📦 MY ORDERS", "💰 MY WALLET"],
            ["👥 REFER & EARN", "💸 WITHDRAW"],
            ["📜 HOW TO USE", "🛡️ CONTACT ADMIN"],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose an FF Store option...",
    )

def referral_code(user_id):
    code = str(user_id)
    referral_data["codes"][code] = int(user_id)
    save_referral_data()
    return code

def referral_link(context, user_id):
    username = context.bot.username or "your_bot"
    return f"https://t.me/{username}?start=ref_{referral_code(user_id)}"

def commission_balance(user_id):
    total = sum(int(v.get("amount", 0)) for v in referral_data["commissions"].values()
                if int(v.get("referrer_id", 0)) == int(user_id) and v.get("status") == "available")
    return total


def wallet_available(user_id):
    """Existing wallet funds plus available, unreserved referral commissions."""
    return wallet_balance(user_id) + commission_balance(user_id)

def spend_wallet_funds(user_id, amount):
    """Debit existing wallet funds first, then referral commission; prevent reuse."""
    amount = int(amount)
    key = str(user_id)
    cash = wallet_balance(user_id)
    commissions = [
        (cid, item) for cid, item in referral_data["commissions"].items()
        if int(item.get("referrer_id", 0)) == int(user_id) and item.get("status") == "available"
    ]
    total = sum(int(item.get("amount", 0)) for _, item in commissions)
    if cash + total < amount:
        return False
    cash_used = min(cash, amount)
    remaining = amount - cash_used
    wallets[key] = cash - cash_used
    now = int(time.time())
    for cid, item in commissions:
        if remaining <= 0:
            break
        value = int(item.get("amount", 0))
        used = min(value, remaining)
        if used == value:
            item["status"] = "spent"
            item["spent_at"] = now
        else:
            item["amount"] = value - used
            spent_id = f"{cid}_SPENT_{now}_{secrets.token_hex(3)}"
            referral_data["commissions"][spent_id] = {
                **item, "amount": used, "status": "spent", "spent_at": now
            }
        remaining -= used
    save_wallets()
    save_referral_data()
    return remaining == 0

def purchase_keyboard():
    rows = []
    for key, (name, price) in PLANS.items():
        rows.append([InlineKeyboardButton(f"{name} — ₹{price}", callback_data=f"plan:{key}")])
    return InlineKeyboardMarkup(rows)

SERVER_REGIONS = [
    ("🇮🇳 IND", "IND"), ("🇧🇩 BD", "BD"), ("🇪🇺 EU", "EU"),
    ("🇮🇩 ID", "ID"), ("🇷🇺 RU", "RU"), ("🇸🇬 SG", "SG"),
    ("🇲🇪 ME", "ME"), ("🇵🇰 PK", "PK"), ("🇻🇳 VN", "VN"),
    ("🇧🇷 BR", "BR"), ("🌎 SAC", "SAC"), ("🌎 NA", "NA"), ("🇺🇸 US", "US"),
]

def region_keyboard(order_id):
    rows, row = [], []
    for label, code in SERVER_REGIONS:
        row.append(InlineKeyboardButton(label, callback_data=f"region:{code}:{order_id}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def method_keyboard(order_id, stage="uid"):
    # Show only the next allowed step; later steps stay locked until validation passes.
    if stage == "uid":
        rows = [[InlineKeyboardButton("🎮 Enter Free Fire UID", callback_data=f"method:uid:{order_id}")]]
    elif stage == "mobile":
        rows = [[InlineKeyboardButton("📱 Enter Mobile Number", callback_data=f"method:mobile:{order_id}")]]
    elif stage == "qr":
        rows = [[InlineKeyboardButton("💳 Payment QR", callback_data=f"method:qr:{order_id}")]]
    else:
        rows = []
    return InlineKeyboardMarkup(rows)

def admin_keyboard(order_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve & Activate", callback_data=f"admin:done:{order_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"admin:reject:{order_id}"),
    ]])

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not update.effective_chat or update.effective_chat.type != "private":
        return

    # First valid referral link wins. Existing attribution is never overwritten.
    if context.args and context.args[0].startswith("ref_"):
        code = context.args[0][4:]
        referrer_id = referral_data["codes"].get(code)
        if referrer_id is None and code.isdigit():
            referrer_id = int(code)
        if referrer_id and int(referrer_id) != user.id and str(user.id) not in referral_data["referrers"]:
            referral_data["referrers"][str(user.id)] = int(referrer_id)
            save_referral_data()

    register_user(user)
    link = referral_link(context, user.id)
    await msg.reply_text(
        "Welcome to FF Store! 🔥\n\n"
        "Purchase menu खोलने के लिए /like purchase भेजें।\n"
        "Group में /like purchase लिखने पर offers आपको यहाँ DM में मिलेंगे।\n\n"
        "🔗 आपका Referral Link:\n" + link + "\n\n"
        "हर सफल और admin-verified order पर 10% commission मिलेगा।\n"
        "Commands: /referral, /balance, /withdraw\n\n"
        "हमारे gaming menu के लिए नीचे दिए keyboard buttons इस्तेमाल करें।",
        reply_markup=main_reply_keyboard()
    )
    await msg.reply_text(
        "🌐 Community links (optional):",
        reply_markup=community_keyboard()
    )

async def keyboard_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route persistent keyboard button labels to existing FF Store features."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not msg.text or not chat or chat.type != "private":
        return

    choice = msg.text.strip()
    known_buttons = {
        "🎮 START GAME", "❤️ RECEIVE 220 LIKE 🔥", "💎 BUY LIKES", "📦 MY ORDERS", "🪙 MY BALANCE", "💰 MY WALLET",
        "👥 REFER & EARN", "💸 WITHDRAW", "📜 HOW TO USE", "🛡️ CONTACT ADMIN",
    }
    if choice not in known_buttons:
        return

    register_user(user)
    if choice == "🎮 START GAME":
        await msg.reply_text(
            "🔥 FF STORE MAX\\nChoose an option from the keyboard below.",
            reply_markup=main_reply_keyboard(),
        )
    elif choice == "❤️ RECEIVE 220 LIKE 🔥":
        await msg.reply_text(
            "❤️ Instant like:\n/Like REGION UID\nExample: /Like ind 3193493636\n\n❤️ 220 Likes = ₹15\n\nअपना Region और UID इसी format में भेजें।",
            reply_markup=main_reply_keyboard(),
        )
    elif choice == "💎 BUY LIKES":
        await msg.reply_text(
            "🛒 FF Store Best Offers\\nअपना प्लान चुनें:",
            reply_markup=purchase_keyboard(),
        )
    elif choice == "📦 MY ORDERS":
        user_orders = [
            order for order in orders.values()
            if int(order.get("user_id", 0)) == user.id
        ]
        if not user_orders:
            text = (
                "📦 MY ORDERS\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "अभी आपका कोई order रिकॉर्ड नहीं है।\n"
                "💎 नया order करने के लिए BUY LIKES दबाएँ।"
            )
        else:
            status_labels = {
                "payment_screenshot_received": "🟠 Payment Screenshot Received",
                "pending_details": "🟡 Details Pending",
                "awaiting_mobile": "🟣 Mobile Number Pending",
                "awaiting_payment": "🟠 Awaiting Payment",
                "completed": "🟢 Completed",
                "pending": "🟡 Pending",
                "processing": "🔵 Processing",
                "cancelled": "🔴 Cancelled",
                "rejected": "🔴 Rejected",
            }
            sorted_orders = sorted(
                user_orders,
                key=lambda x: int(x.get("created_at", 0)),
                reverse=True,
            )[:10]
            lines = [
                "📦 MY ORDERS",
                "━━━━━━━━━━━━━━━━━━",
                "Your latest orders (max 10):",
            ]
            for index, order in enumerate(sorted_orders, start=1):
                raw_status = str(order.get("status", "unknown"))
                status = status_labels.get(raw_status, f"⚪ {raw_status.replace('_', ' ').title()}")
                lines.append(
                    f"\n{index}. 🧾 Order ID: {order.get('order_id', '-')}\n"
                    f"   👍 Plan: {order.get('plan', '-')}\n"
                    f"   💰 Price: ₹{order.get('price', 0)}\n"
                    f"   📌 Status: {status}\n"
                    "━━━━━━━━━━━━━━━━━━"
                )
            text = "\n".join(lines)
        await msg.reply_text(text, reply_markup=main_reply_keyboard())
    elif choice in ("🪙 MY BALANCE", "💰 MY WALLET"):
        await balance_handler(update, context)
    elif choice == "💰 ADD BALANCE":
        await msg.reply_text("💰 Add Balance — UPI (₹)\nMinimum: ₹30\nSelect an amount or choose Custom Amount:", reply_markup=topup_amount_keyboard())
    elif choice == "👥 REFER & EARN":
        await referral_handler(update, context)
    elif choice == "💸 WITHDRAW":
        await withdraw_handler(update, context)
    elif choice == "📜 HOW TO USE":
        await howtouse_handler(update, context)
    elif choice == "🛡️ CONTACT ADMIN":
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "🛡️ FF Store Contact Admin request\n"
                    f"User: {user.full_name} (@{user.username or '-'})\n"
                    f"User ID: {user.id}\n"
                    "Please contact this user in Telegram if needed."
                ),
            )
            await msg.reply_text(
                "✅ Admin को आपकी contact request भेज दी गई है।",
                reply_markup=main_reply_keyboard(),
            )
        except Exception:
            await msg.reply_text(
                "⚠️ Admin को message नहीं भेज पाया। कृपया थोड़ी देर बाद कोशिश करें।",
                reply_markup=main_reply_keyboard(),
            )
    raise ApplicationHandlerStop


async def howtouse_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the Free Fire Like Assistant tutorial video."""
    msg = update.effective_message
    if msg:
        await msg.reply_text(
            "📘 FF Store Bot — How to Use\n\n"
            "▶️ Tutorial video: https://youtu.be/iBXbPRZR4qY?si=9NEwvrr6U6nxZ_T8"
        )


async def referral_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not update.effective_chat or update.effective_chat.type != "private":
        return
    link = referral_link(context, user.id)
    referred = sum(1 for rid in referral_data["referrers"].values() if int(rid) == user.id)
    balance = commission_balance(user.id)
    await msg.reply_text(
        f"🔗 आपका referral link:\n{link}\n\n"
        f"👥 Referred users: {referred}\n"
        f"💰 Available commission: ₹{balance}\n"
        f"📤 Minimum withdrawal: ₹{MIN_WITHDRAWAL}"
    )

async def balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and update.effective_message and update.effective_chat.type == "private":
        added = sum(int(x.get("amount", 0)) for x in wallet_topups.values()
                    if int(x.get("user_id", 0)) == user.id and x.get("status") in ("approved", "credited"))
        await update.effective_message.reply_text(
            f"💰 MY WALLET\n━━━━━━━━━━━━━━━━━━\n"
            f"Available Balance: ₹{wallet_available(user.id)}\n"
            f"👥 Referral Earnings Available: ₹{commission_balance(user.id)}\n"
            f"➕ Total Added by UPI: ₹{added}\n"
            f"💵 Wallet Cash: ₹{wallet_balance(user.id)}\n\n"
            "नीचे दिए बटन से Wallet में पैसे जोड़ें।",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ ADD BALANCE", callback_data="topup:open")]])
        )

async def withdraw_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not update.effective_chat or update.effective_chat.type != "private":
        return
    balance = commission_balance(user.id)
    if balance < MIN_WITHDRAWAL:
        await msg.reply_text(f"निकासी के लिए कम से कम ₹{MIN_WITHDRAWAL} चाहिए। आपका balance ₹{balance} है।")
        return
    existing = [w for w in referral_data["withdrawals"].values()
                if int(w.get("user_id", 0)) == user.id and w.get("status") == "pending"]
    if existing:
        await msg.reply_text("आपकी एक withdrawal request पहले से pending है। Admin के निर्णय का इंतज़ार करें।")
        return
    context.user_data["awaiting"] = "withdrawal_upi"
    await msg.reply_text(
        f"आपका उपलब्ध कमीशन ₹{balance} है।\n"
        "कृपया अपना UPI ID भेजें (उदाहरण: name@upi)।\n"
        "UPI ID मिलने के बाद ही request admin को भेजी जाएगी।"
    )

async def purchase_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    if not re.fullmatch(r"/like(?:@\w+)?(?:\s+purchase)?", msg.text.strip(), re.I):
        return

    if update.effective_chat.type == "private":
        await msg.reply_text("🛒 **FF Store Best Offers**\nअपना प्लान चुनें:", reply_markup=purchase_keyboard(), parse_mode="Markdown")
        raise ApplicationHandlerStop

    # In group: keep the notice short; purchase choices are sent privately.
    user = update.effective_user
    if not user:
        return
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text="🎉 आपको DM में Best Offer मिला है! अभी चेक करो। 🔥\n\n🛒 अपना Purchase Plan चुनें:",
            reply_markup=purchase_keyboard(),
        )
        await msg.reply_text("🎉 आपको DM में Best Offer मिला है! अभी चेक करो। 🔥")
    except Exception:
        # Telegram bots cannot start a private conversation until the user presses Start.
        username = context.bot.username or "your_bot"
        await msg.reply_text(
            f"पहले बॉट के DM में Start दबाएँ: https://t.me/{username}\n"
            "फिर ग्रुप में /like या /like purchase लिखें।"
        )

async def _send_like_command_with_reconnect(region_code: str, uid: str):
    """Send once; if Telethon is disconnected, reconnect and retry once."""
    command = f"/like {region_code} {uid}"
    try:
        return await client.send_message(TARGET_GROUP_ID, command)
    except Exception:
        # The screenshot's error occurs when Telethon is no longer connected.
        # Reconnect only when disconnected; do not blindly resend an ambiguous
        # request while the client still reports connected.
        if client.is_connected():
            raise
        await client.connect()
        if not await client.is_user_authorized():
            await client.start(phone=PHONE)
        return await client.send_message(TARGET_GROUP_ID, command)


async def like_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    print(
        "LIKE HANDLER MATCHED | chat_id=", getattr(chat, "id", None),
        "| chat_type=", getattr(chat, "type", None),
        "| text=", repr(getattr(msg, "text", None)),
        flush=True,
    )
    if not msg or not msg.text:
        return

    command_text = msg.text.strip()
    # Paid 220-like orders are handled only in the private bot chat; group /like relay stays unchanged.
    if chat and chat.type == "private":
        paid_match = re.fullmatch(r"/like(?:@\w+)?\s+(IND|BD|EU|ID|RU|SG|ME|PK|VN)\s+(\d{8,12})", command_text, re.I)
        if paid_match:
            region_code, uid = paid_match.group(1).upper(), paid_match.group(2)
            user = update.effective_user
            register_user(user)
            insufficient_balance = False
            debit_failed = False
            order_id = None
            remaining = 0
            with wallet_lock:
                available = wallet_available(user.id)
                if available < 15:
                    insufficient_balance = True
                elif not spend_wallet_funds(user.id, 15):
                    debit_failed = True
                else:
                    order_id = "L220" + secrets.token_hex(4).upper()
                    orders[order_id] = {
                        "order_id": order_id, "user_id": user.id, "username": user.username or "",
                        "name": user.full_name or "", "plan": "220 Likes (Manual Delivery)", "price": 15,
                        "server_region": region_code, "ff_uid": uid, "status": "pending",
                        "payment_method": "wallet", "wallet_debited": True, "wallet_debited_at": int(time.time()),
                        "created_at": int(time.time()), "manual_220": True, "referral_commissioned": False,
                    }
                    save_json(ORDERS_FILE, orders)
                    remaining = wallet_available(user.id)
            if insufficient_balance:
                await msg.reply_text(
                    f"⚠️ Insufficient Wallet Balance!\\n\\n❤️ 220 Likes Price: ₹15\\n💰 Your Balance: ₹{available}\\n\\n➕ Please Add Balance to continue.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ ADD BALANCE", callback_data="topup:open")]])
                )
                return
            if debit_failed:
                await msg.reply_text("⚠️ Wallet balance update नहीं हो पाया। कृपया दोबारा कोशिश करें।")
                return
            await msg.reply_text(
                f"🟡 ORDER PENDING\n❤️ FF STORE — 220 LIKES\n\n🧾 Order ID: {order_id}\n🌍 Region: {region_code}\n🎮 UID: {uid}\n💳 Amount: ₹15 (Wallet)\n💰 Remaining Balance: ₹{remaining}\n\nYour order has been received. Likes will be delivered manually by our admin. No fixed delivery time."
            )
            admin_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔵 PROCESSING", callback_data=f"manual:processing:{order_id}")],
                [InlineKeyboardButton("🟢 MARK COMPLETED", callback_data=f"manual:complete:{order_id}")],
                [InlineKeyboardButton("🔴 CANCEL & REFUND", callback_data=f"manual:cancel:{order_id}")],
            ])
            await context.bot.send_message(ADMIN_ID,
                f"❤️ NEW 220 LIKES ORDER\nOrder: {order_id}\nUser: {user.full_name} (@{user.username or '-'})\nUser ID: {user.id}\nRegion: {region_code}\nUID: {uid}\nDeducted: ₹15\nWallet left: ₹{remaining}\nStatus: PENDING",
                reply_markup=admin_kb)
            return
    match = re.fullmatch(r"/like(?:@\w+)?\s+(IND|BD|EU|ID|RU|SG|ME|PK|VN)\s+(\d{8,12})", command_text, re.I)
    if not match:
        # Keep the existing free-like validation response for plain /like or invalid format.
        if re.fullmatch(r"/like(?:@\w+)?(?:\s+.*)?", command_text, re.I):
            await msg.reply_text(
                "❌ Wrong format — Missing: REGION, UID\n\n"
                "📌 Usage:\n/like REGION UID\n\n"
                "✅ Example:\n/like ind 1899492711\n\n"
                "💡 REGION = ind, bd, eu, id, ru, sg, me, pk or vn.\n"
                "UID = your Free Fire UID."
            )
        return

    region_code = match.group(1).upper()
    uid = match.group(2)

    # Serialize attempts for the same UID so two simultaneous messages cannot
    # both pass the cooldown check and send duplicate commands.
    lock = uid_locks.setdefault(uid, asyncio.Lock())
    async with lock:
        now = time.time()
        last_used = float(uid_history.get(uid, 0))

        if now - last_used < COOLDOWN_SECONDS:
            remaining = max(0, int(COOLDOWN_SECONDS - (now - last_used)))
            hours, remainder = divmod(remaining, 3600)
            minutes = remainder // 60
            await msg.reply_text(
                "⏳ FREE LIKES COOLDOWN!\n\n"
                "This UID has already received free likes.\n\n"
                f"🎮 UID: {uid}\n"
                f"⏰ Time Left: {hours} Hours {minutes} Minutes"
            )
            try:
                await msg.reply_photo(
                    photo=DUPLICATE_IMAGE,
                    caption=(
                        "🔥 Want More Likes?\n"
                        "Get more likes from FF Store!\n\n"
                        "🛒 Buy Likes: https://t.me/FFStoreLikeBot"
                    ),
                    reply_to_message_id=msg.message_id,
                )
            except Exception as e:
                await msg.reply_text(
                    "🔥 Want More Likes? Get more likes from FF Store!\n"
                    "🛒 Buy Likes: https://t.me/FFStoreLikeBot\n"
                    f"(Offer image could not be sent: {type(e).__name__})"
                )
            return

        try:
            # IMPORTANT: Do not save cooldown before the target-group send.
            sent = await _send_like_command_with_reconnect(region_code, uid)
        except Exception as e:
            # Failed sends never start or consume this UID's cooldown.
            await msg.reply_text(f"Send error: {type(e).__name__}: {e}")
            return

        # The target group accepted the command; only now start the cooldown.
        uid_history[uid] = time.time()
        save_json(UID_HISTORY_FILE, uid_history)

        pending[sent.id] = {
            "source_chat": update.effective_chat.id,
            "source_msg": msg.message_id,
            "uid": uid,
        }
        await msg.reply_text(
            "Free likes have been sent! Open Free Fire to check.\n"
            "Note: You can receive free likes only once per day. "
            "Come back tomorrow for more likes.\n"
            "Group join करना optional है — updates के लिए नीचे FF Store Group join कर सकते हैं: https://t.me/FFISTORE"
        )


def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Orders", callback_data="adminpanel:orders"),
         InlineKeyboardButton("💸 Sales", callback_data="adminpanel:sales")],
        [InlineKeyboardButton("👥 Referrals", callback_data="adminpanel:referrals"),
         InlineKeyboardButton("🏦 Withdrawals", callback_data="adminpanel:withdrawals")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="adminpanel:home")],
    ])

async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if user.id != ADMIN_ID:
        await msg.reply_text("⛔ यह command सिर्फ admin के लिए है।")
        return
    text = " ".join(context.args).strip()
    if not text:
        await msg.reply_text("इस्तेमाल: /broadcast आपका मैसेज\n\nउदाहरण: /broadcast आज नए offers उपलब्ध हैं!")
        return
    context.user_data["pending_broadcast"] = text
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Send to registered users", callback_data="broadcast:confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="broadcast:cancel"),
    ]])
    await msg.reply_text(
        f"📣 Broadcast preview:\n\n{text}\n\n"
        f"Recipients: {len(registered_users)} registered users\nक्या भेजना है?",
        reply_markup=keyboard,
    )

async def broadcast_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if query.from_user.id != ADMIN_ID:
        await query.answer("यह बटन सिर्फ admin के लिए है।", show_alert=True)
        return
    await query.answer()
    if query.data == "broadcast:cancel":
        context.user_data.pop("pending_broadcast", None)
        await query.edit_message_text("❌ Broadcast cancel कर दिया गया।")
        return
    if query.data == "broadcast:confirm":
        text = context.user_data.pop("pending_broadcast", None)
        if not text:
            await query.edit_message_text("Broadcast text नहीं मिला। दोबारा /broadcast इस्तेमाल करें।")
            return
        sent = 0
        failed = 0
        for user_id in list(registered_users.keys()):
            try:
                await context.bot.send_message(chat_id=int(user_id), text=text)
                sent += 1
            except Exception as exc:
                failed += 1
                print(f"Broadcast failed for user {user_id}: {exc}")
        await query.edit_message_text(f"📣 Broadcast पूरा हुआ।\n✅ Sent: {sent}\n⚠️ Failed/blocked: {failed}")

async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if user.id != ADMIN_ID:
        await msg.reply_text("⛔ Access Denied! यह कमांड सिर्फ Admin के लिए है।")
        return
    await msg.reply_text(
        "🔐 FF STORE ADMIN PANEL\\n\\nनीचे से सेक्शन चुनें:",
        reply_markup=admin_panel_keyboard()
    )

def admin_orders_summary(page=0, page_size=10):
    all_orders = sorted(
        orders.values(),
        key=lambda x: int(x.get("created_at", 0) or 0),
        reverse=True
    )
    total = len(all_orders)
    if total == 0:
        return "📦 अभी कोई order रिकॉर्ड नहीं है।", None

    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(int(page), total_pages - 1))
    start = page * page_size
    current_orders = all_orders[start:start + page_size]
    lines = [
        f"📦 ALL ORDERS",
        f"Total Orders: {total} | Page {page + 1}/{total_pages}",
        "━━━━━━━━━━━━━━━━━━"
    ]
    for order in current_orders:
        created_at = order.get("created_at")
        try:
            created_text = time.strftime("%d-%m-%Y %I:%M %p", time.localtime(int(created_at)))
        except (TypeError, ValueError, OSError):
            created_text = "Time not available"
        lines.append(
            f"🧾 Order: {order.get('order_id', '-')}\n"
            f"📦 Plan: {order.get('plan', '-')} | 💰 Amount: ₹{order.get('price', 0)}\n"
            f"📅 Date & Time: {created_text}\n"
            f"📌 Status: {order.get('status', 'unknown')}\n"
            f"👤 User ID: {order.get('user_id', '-')}\n"
            f"🌍 Server Region: {order.get('server_region') or 'Not selected'}\n"
            f"🎮 UID: {order.get('ff_uid') or 'Not provided'}\n"
            f"📱 Mobile: {order.get('mobile') or 'Not provided'}\n"
            "━━━━━━━━━━━━━━━━━━"
        )
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adminpanel:orders:{page-1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="adminpanel:orders:info"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adminpanel:orders:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="adminpanel:home")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)

def admin_referrals_summary():
    referrers = referral_data.get("referrers", {})
    commissions = referral_data.get("commissions", {})
    total_referred = len(referrers)
    available = sum(int(v.get("amount", 0)) for v in commissions.values() if v.get("status") == "available")
    reserved = sum(int(v.get("amount", 0)) for v in commissions.values() if v.get("status") == "withdrawal_pending")
    paid = sum(int(v.get("amount", 0)) for v in commissions.values() if v.get("status") == "paid_out")
    lines = [
        "👥 REFERRAL REPORT",
        f"Referred users: {total_referred}",
        f"Commission records: {len(commissions)}",
        f"Available commission total: ₹{available}",
        f"Withdrawal-pending commission: ₹{reserved}",
        f"Paid-out commission: ₹{paid}",
    ]
    if referrers:
        lines.append("\\nLatest referral mappings (max 10):")
        for referred_user, referrer_id in list(referrers.items())[-10:]:
            lines.append(f"User {referred_user} → Referrer {referrer_id}")
    return "\n".join(lines)

def admin_withdrawals_summary():
    withdrawals = referral_data.get("withdrawals", {})
    pending_items = [(wid, w) for wid, w in withdrawals.items() if w.get("status") == "pending"]
    if not pending_items:
        return "🏦 कोई pending withdrawal request नहीं है।"
    lines = ["🏦 PENDING WITHDRAWALS"]
    for wid, w in pending_items[-10:][::-1]:
        lines.append(
            f"\\nID: {wid}\\nUser ID: {w.get('user_id', '-')} | Amount: ₹{w.get('amount', 0)}"
            f"\\nUPI: {w.get('upi_id', '-')}\\nStatus: {w.get('status', '-')}"
        )
    return "\n".join(lines)

def admin_sales_summary():
    all_orders = list(orders.values())
    completed = [o for o in all_orders if o.get("status") == "completed"]
    paid_or_completed = [o for o in all_orders if o.get("status") in ("paid", "completed")]
    pending = [o for o in all_orders if o.get("status") not in ("completed",)]
    gross_completed = sum(int(o.get("price", 0)) for o in completed)
    gross_verified = sum(int(o.get("price", 0)) for o in paid_or_completed)
    return (
        "💸 SALES REPORT\n"
        f"Total orders: {len(all_orders)}\n"
        f"Completed orders: {len(completed)}\n"
        f"Paid or completed orders: {len(paid_or_completed)}\n"
        f"Other/in-progress orders: {len(pending)}\n"
        f"Completed order value: ₹{gross_completed}\n"
        f"Verified order value (paid + completed): ₹{gross_verified}\\n\n"
        "नोट: ये order records के आधार पर रकम है, बैंक/UPI से independently verified sales नहीं।"
    )

async def begin_wallet_topup(query, context, amount):
    user = query.from_user
    if amount < 30:
        await query.answer("Minimum recharge ₹30 है।", show_alert=True); return
    topup_id = "WT" + secrets.token_hex(4).upper()
    wallet_topups[topup_id] = {
        "topup_id": topup_id, "user_id": user.id, "name": user.full_name or "",
        "username": user.username or "", "amount": int(amount), "status": "pending",
        "created_at": int(time.time()), "payment_method": "phonepe_upi_qr",
    }
    save_wallet_topups()
    context.user_data["current_topup"] = topup_id
    context.user_data.pop("awaiting", None)
    caption = (f"💰 FF STORE WALLET RECHARGE\nAmount: ₹{amount}\nReference: {topup_id}\n\n"
               "PhonePe खोलकर QR scan करें और बिल्कुल यही amount pay करें। Payment के बाद screenshot इसी chat में भेजें। "
               "Admin आपके UPI में वास्तविक payment verify करके ही Wallet credit करेगा।")
    try:
        with open(QR_PATH, "rb") as qr:
            await context.bot.send_photo(chat_id=user.id, photo=qr, caption=caption)
    except OSError:
        await context.bot.send_message(chat_id=user.id, text=caption + "\n⚠️ QR image file नहीं मिली। Admin से संपर्क करें।")

async def purchase_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""

    if data == "topup:open":
        await query.message.reply_text("💰 Add Balance — UPI (₹)\nMinimum: ₹30\nSelect an amount or enter your own:", reply_markup=topup_amount_keyboard())
        return
    if data.startswith("topup:"):
        bits = data.split(":")
        if len(bits) >= 3 and bits[1] == "amount":
            amount = int(bits[2])
            if amount < 30 or amount > 50000:
                await query.answer("Amount ₹30 से ₹50,000 के बीच होना चाहिए।", show_alert=True); return
            await begin_wallet_topup(query, context, amount)
            return
        if len(bits) == 2 and bits[1] == "custom":
            context.user_data["awaiting"] = "wallet_topup_amount"
            await query.message.reply_text("अपनी recharge amount भेजें (Minimum ₹30, Maximum ₹50,000).")
            return

    if data.startswith("manual:"):
        _, action, order_id = data.split(":", 2)
        if query.from_user.id != ADMIN_ID:
            await query.answer("यह action सिर्फ Admin के लिए है।", show_alert=True); return
        order = orders.get(order_id)
        if not order or not order.get("manual_220"):
            await query.answer("Manual order नहीं मिला।", show_alert=True); return
        status = order.get("status")
        if action == "processing":
            if status not in ("pending", "processing"):
                await query.answer("यह order अब update नहीं हो सकता।", show_alert=True); return
            order["status"] = "processing"; order["processing_at"] = int(time.time()); save_json(ORDERS_FILE, orders)
            await context.bot.send_message(order["user_id"], f"🔵 ORDER PROCESSING\nOrder ID: {order_id}\nआपके 220 Likes की प्रक्रिया शुरू कर दी गई है।")
            await query.edit_message_text((query.message.text or "") + "\n\n🔵 STATUS: PROCESSING")
            return
        if action == "complete":
            if status not in ("pending", "processing"):
                await query.answer("Order पहले ही process हो चुका है।", show_alert=True); return
            order["status"] = "completed"; order["completed_at"] = int(time.time()); save_json(ORDERS_FILE, orders)
            await context.bot.send_message(order["user_id"], f"🟢 ORDER COMPLETED\n❤️ 220 Likes successfully delivered!\nOrder ID: {order_id}\nUID: {order.get('ff_uid')}\nThank you for choosing FF STORE!")
            await query.edit_message_text((query.message.text or "") + "\n\n🟢 STATUS: COMPLETED")
            return
        if action == "cancel":
            if status in ("cancelled", "refunded", "completed"):
                await query.answer("Order पहले ही बंद हो चुका है।", show_alert=True); return
            with wallet_lock:
                if not order.get("refunded"):
                    wallets[str(order["user_id"])] = wallet_balance(order["user_id"]) + 15
                    order["refunded"] = True
                    order["refunded_at"] = int(time.time())
                    save_wallets()
                order["status"] = "cancelled"; save_json(ORDERS_FILE, orders)
                newbal = wallet_available(order["user_id"])
            await context.bot.send_message(order["user_id"], f"🔴 ORDER CANCELLED\nOrder ID: {order_id}\n₹15 आपके Wallet में वापस कर दिए गए हैं।\nNew balance: ₹{newbal}")
            await query.edit_message_text((query.message.text or "") + "\n\n🔴 CANCELLED — ₹15 REFUNDED")
            return

    if data.startswith("wallet:"):
        parts = data.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        ident = parts[2] if len(parts) > 2 else ""
        user_id = query.from_user.id
        if action == "add":
            await query.answer("Wallet top-up बंद है। Referral balance से plan खरीदें।", show_alert=True)
            return
        if action == "pay":
            order = orders.get(ident)
            if not order or int(order.get("user_id", 0)) != user_id:
                await query.message.reply_text("Order नहीं मिला। कृपया फिर से प्लान चुनें।"); return
            if order.get("wallet_debited"):
                await query.message.reply_text("इस order का wallet payment पहले हो चुका है।"); return
            price = int(order.get("price", 0))
            with wallet_lock:
                balance = wallet_available(user_id)
                if balance >= price:
                    if spend_wallet_funds(user_id, price):
                        order["wallet_debited"] = True
                        order["payment_method"] = "wallet"
                        order["wallet_debited_at"] = int(time.time())
                        order["status"] = "paid"
                        save_json(ORDERS_FILE, orders)
                    else:
                        balance = wallet_available(user_id)
            if balance < price:
                await query.answer(f"Available ₹{balance}; plan price ₹{price}. QR payment चुनें।", show_alert=True)
                await query.message.reply_text(
                    f"⚠️ Wallet + referral balance कम है।\\nAvailable: ₹{balance}\n"
                    f"Plan price: ₹{price}\\nQR payment के लिए नीचे बटन दबाएँ।",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("💳 PAY BY QR", callback_data=f"method:qr:{ident}")
                    ]])
                )
                return
            await query.edit_message_text(f"✅ Wallet payment successful!\nOrder: {ident}\nPlan: {order['plan']}\nDeducted: ₹{price}\nRemaining spendable balance: ₹{wallet_available(user_id)}\n\nAdmin अब order process करेगा।")
            await send_admin_order(context, ident)
            await context.bot.send_message(ADMIN_ID, f"🪙 WALLET ORDER PAID\nOrder: {ident}\nUser ID: {user_id}\nPlan: {order['plan']}\nAmount deducted: ₹{price}\nSpendable balance left: ₹{wallet_available(user_id)}")
            return
        if action in ("approve", "reject"):
            if user_id != ADMIN_ID:
                await query.message.reply_text("यह बटन सिर्फ admin के लिए है।"); return
            topup = wallet_topups.get(ident)
            if not topup or topup.get("status") != "pending":
                await query.message.reply_text("यह top-up पहले ही process हो चुका है।"); return
            if action == "approve":
                if not topup.get("screenshot_file_id"):
                    await query.answer("पहले payment screenshot और वास्तविक UPI credit verify करें।", show_alert=True); return
                target = str(topup["user_id"])
                wallets[target] = wallet_balance(int(target)) + int(topup["amount"])
                topup["status"] = "approved"
                topup["approved_at"] = int(time.time())
                save_wallets(); save_wallet_topups()
                await context.bot.send_message(int(target), f"✅ Wallet top-up approved!\nAdded: ₹{topup['amount']}\nNew available balance: ₹{wallet_available(int(target))}\nReference: {ident}")
                await context.bot.send_message(ADMIN_ID, f"🔔 WALLET CREDITED\nUser ID: {target}\nAmount: ₹{topup['amount']}\nReference: {ident}\nNew wallet cash: ₹{wallet_balance(int(target))}")
                await query.edit_message_caption(caption=(query.message.caption or "") + "\n\n✅ APPROVED — WALLET CREDITED") if query.message.photo else await query.edit_message_text((query.message.text or "") + "\n\n✅ APPROVED — WALLET CREDITED")
            else:
                topup["status"] = "rejected"; topup["rejected_at"] = int(time.time()); save_wallet_topups()
                await context.bot.send_message(int(topup["user_id"]), f"❌ Wallet top-up request {ident} reject हुई। अगर आपने payment किया है तो admin से संपर्क करें।")
                await query.edit_message_caption(caption=(query.message.caption or "") + "\n\n❌ REJECTED") if query.message.photo else await query.edit_message_text((query.message.text or "") + "\n\n❌ REJECTED")
            return

    if data.startswith("adminpanel:"):
        if query.from_user.id != ADMIN_ID:
            await query.answer("यह बटन सिर्फ admin के लिए है।", show_alert=True)
            return
        parts = data.split(":")
        section = parts[1] if len(parts) > 1 else "home"
        if section == "orders":
            if len(parts) > 2 and parts[2].isdigit():
                page = int(parts[2])
            else:
                page = 0
            text, markup = admin_orders_summary(page=page)
            await query.edit_message_text(text, reply_markup=markup)
            return
        elif section == "sales":
            text = admin_sales_summary()
        elif section == "referrals":
            text = admin_referrals_summary()
        elif section == "withdrawals":
            text = admin_withdrawals_summary()
        else:
            text = "🔐 FF STORE ADMIN PANEL\\n\\nनीचे से सेक्शन चुनें:"
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard())
        return


    if data.startswith("plan:"):
        register_user(query.from_user)
        key = data.split(":", 1)[1]
        if key not in PLANS:
            await query.edit_message_text("यह प्लान उपलब्ध नहीं है।")
            return
        plan_name, price = PLANS[key]
        order_id = "FF" + secrets.token_hex(4).upper()
        orders[order_id] = {
            "order_id": order_id,
            "user_id": query.from_user.id,
            "username": query.from_user.username or "",
            "name": query.from_user.full_name,
            "plan": plan_name,
            "plan_key": key,
            "price": price,
            "status": "pending_details",
            "created_at": int(time.time()),
            "referrer_id": referral_data["referrers"].get(str(query.from_user.id)),
            "referral_commissioned": False,
        }
        save_json(ORDERS_FILE, orders)
        context.user_data["current_order"] = order_id
        await query.edit_message_text(
            f"✅ आपने चुना: {plan_name}\n💰 कीमत: ₹{price}\n🧾 Order ID: {order_id}\n\n🌍 अपना Server Region चुनें:",
            reply_markup=region_keyboard(order_id),
        )
        return

    if data.startswith("region:"):
        _, region_code, order_id = data.split(":", 2)
        order = orders.get(order_id)
        if not order or int(order.get("user_id", 0)) != query.from_user.id:
            await query.edit_message_text("Order नहीं मिला। कृपया फिर से प्लान चुनें।")
            return
        if region_code not in {code for _, code in SERVER_REGIONS}:
            await query.answer("Invalid server region.", show_alert=True)
            return
        order["server_region"] = region_code
        order["status"] = "region_selected"
        save_json(ORDERS_FILE, orders)
        context.user_data["current_order"] = order_id
        await query.edit_message_text(
            f"✅ Server Region चुना गया: {region_code}\n🧾 Order ID: {order_id}\n\nअब अपना Free Fire UID submit करें:",
            reply_markup=method_keyboard(order_id, "uid"),
        )
        return

    if data.startswith("method:"):
        _, method, order_id = data.split(":", 2)
        order = orders.get(order_id)
        if not order or order["user_id"] != query.from_user.id:
            await query.edit_message_text("Order नहीं मिला। कृपया फिर से प्लान चुनें।")
            return
        if method == "uid":
            if not order.get("server_region"):
                await query.answer("पहले Server Region चुनें।", show_alert=True)
                return
            order["status"] = "awaiting_uid"
            save_json(ORDERS_FILE, orders)
            context.user_data["current_order"] = order_id
            context.user_data["awaiting"] = "uid"
            await query.edit_message_text(f"अपना Free Fire UID भेजें (8–11 अंक)।\nOrder ID: {order_id}")
            return
        if method == "mobile":
            if not order.get("ff_uid"):
                await query.answer("पहले valid UID submit करें।", show_alert=True)
                return
            order["status"] = "awaiting_mobile"
            save_json(ORDERS_FILE, orders)
            context.user_data["current_order"] = order_id
            context.user_data["awaiting"] = "mobile"
            await query.edit_message_text(f"अपना 10 अंकों का mobile number भेजें।\nOrder ID: {order_id}")
            return
        if method == "qr":
            if not order.get("ff_uid") or not order.get("mobile"):
                await query.answer("पहले valid UID और mobile number submit करें।", show_alert=True)
                return
            order["status"] = "awaiting_payment"
            save_json(ORDERS_FILE, orders)
            caption = (
                f"💳 **Payment Details**\nOrder ID: `{order_id}`\n"
                f"Plan: {order['plan']}\nAmount: ₹{order['price']}\n\n"
                "QR scan करके सही amount pay करें। Payment के बाद screenshot इसी chat में भेजें। "
                "Admin payment verify करेगा।"
            )
            qr_path = PLAN_QR_PATHS.get(order.get("plan_key"))
            try:
                if not qr_path or not qr_path.is_file():
                    raise FileNotFoundError(str(qr_path))
                with open(qr_path, "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=query.from_user.id, photo=photo, caption=caption, parse_mode="Markdown"
                    )
            except FileNotFoundError:
                await context.bot.send_message(
                    chat_id=query.from_user.id,
                    text=caption + f"\n\nइस प्लान की QR image नहीं मिली: {qr_path}",
                    parse_mode="Markdown",
                )
            # Admin approval is deferred until payment screenshot submission.
            return

    if data.startswith("withdraw:"):
        _, action, withdrawal_id = data.split(":", 2)
        if query.from_user.id != ADMIN_ID:
            await query.answer("यह बटन सिर्फ admin के लिए है।", show_alert=True)
            return
        withdrawal = referral_data["withdrawals"].get(withdrawal_id)
        if not withdrawal or withdrawal.get("status") != "pending":
            await query.answer("यह request पहले ही process हो चुकी है।", show_alert=True)
            return
        if action == "paid":
            withdrawal["status"] = "paid"
            withdrawal["paid_at"] = int(time.time())
            # Only consume the exact commissions reserved for this withdrawal.
            for cid in withdrawal.get("commission_ids", []):
                item = referral_data["commissions"].get(cid)
                if item and item.get("status") == "withdrawal_pending" and item.get("withdrawal_id") == withdrawal_id:
                    item["status"] = "paid_out"
            save_referral_data()
            await context.bot.send_message(withdrawal["user_id"], f"✅ Referral payout marked paid.\nRequest: {withdrawal_id}\nAmount: ₹{withdrawal['amount']}")
            await query.edit_message_text((query.message.text or "") + "\n\n✅ STATUS: PAID")
        elif action == "reject":
            withdrawal["status"] = "rejected"
            for cid in withdrawal.get("commission_ids", []):
                item = referral_data["commissions"].get(cid)
                if item and item.get("status") == "withdrawal_pending" and item.get("withdrawal_id") == withdrawal_id:
                    item["status"] = "available"
                    item.pop("withdrawal_id", None)
            save_referral_data()
            await context.bot.send_message(withdrawal["user_id"], f"❌ Withdrawal request {withdrawal_id} reject हुई है। आपका कमीशन balance में वापस जोड़ दिया गया है।")
            await query.edit_message_text((query.message.text or "") + "\n\n❌ STATUS: REJECTED")
        return

    if data.startswith("admin:"):
        _, action, order_id = data.split(":", 2)
        if query.from_user.id != ADMIN_ID:
            await query.answer("यह बटन सिर्फ admin के लिए है।", show_alert=True)
            return
        order = orders.get(order_id)
        if not order:
            await query.edit_message_text("Order नहीं मिला।")
            return
        if action == "paid":
            if order.get("status") in ("paid", "completed"):
                await query.answer("Payment पहले ही mark हो चुका है।", show_alert=True)
                return
            order["status"] = "paid"
            save_json(ORDERS_FILE, orders)
            await context.bot.send_message(order["user_id"], f"✅ Payment marked as received.\nOrder ID: {order_id}\nAdmin अब order process करेगा।")
            await query.edit_message_reply_markup(reply_markup=admin_keyboard(order_id))
            await query.answer("Payment marked paid")
        elif action == "reject":
            if order.get("status") in ("completed", "rejected"):
                await query.answer("यह order पहले ही process हो चुका है।", show_alert=True)
                return
            if order.get("status") != "payment_screenshot_received":
                await query.answer("इस order का payment screenshot pending नहीं है।", show_alert=True)
                return
            order["status"] = "rejected"
            order["rejected_at"] = int(time.time())
            save_json(ORDERS_FILE, orders)
            await context.bot.send_message(
                order["user_id"],
                f"❌ आपका payment screenshot reject कर दिया गया है।\\nOrder ID: {order_id}\\nकृपया सही payment screenshot के साथ Admin से संपर्क करें।"
            )
            if query.message.photo:
                await query.edit_message_caption(caption=(query.message.caption or "") + "\\n\\n❌ REJECTED")
            else:
                await query.edit_message_text((query.message.text or "") + "\\n\\n❌ REJECTED")
            return
        elif action == "done":
            if order.get("status") not in ("paid", "completed", "payment_screenshot_received"):
                await query.answer("पहले payment screenshot प्राप्त होना चाहिए।", show_alert=True)
                return
            if order.get("status") == "completed":
                await query.answer("Order पहले ही completed है।", show_alert=True)
                return
            order["status"] = "completed"
            # Plan starts the day after admin approval; count the start date as Day 1.
            approved_local_date = datetime.now(IST).date()
            start_date = approved_local_date + timedelta(days=1)
            duration_days = plan_duration_days(order)
            expiry_date = start_date + timedelta(days=duration_days - 1)
            order["approved_at"] = int(time.time())
            order["plan_start_date"] = start_date.isoformat()
            order["plan_expiry_date"] = expiry_date.isoformat()
            # Replacing this record cancels reminders for the user's previous plan.
            active_like_plans[str(order["user_id"])] = {
                "user_id": int(order["user_id"]), "order_id": order_id,
                "plan": order.get("plan", "Like Plan"),
                "start_date": start_date.isoformat(),
                "expiry_date": expiry_date.isoformat(),
                "reminder_sent_dates": [], "expired_notice_sent": False,
                "activated_at": int(time.time())
            }
            save_active_like_plans()
            if not order.get("referral_commissioned"):
                referrer_id = order.get("referrer_id")
                if referrer_id and int(referrer_id) != int(order["user_id"]):
                    commission_id = f"{order_id}_REF"
                    if commission_id not in referral_data["commissions"]:
                        referral_data["commissions"][commission_id] = {
                            "order_id": order_id, "referrer_id": int(referrer_id),
                            "buyer_id": int(order["user_id"]),
                            "amount": int(order["price"] * COMMISSION_RATE),
                            "status": "available", "created_at": int(time.time())
                        }
                    order["referral_commissioned"] = True
                    save_referral_data()
                    try:
                        await context.bot.send_message(int(referrer_id),
                            f"🎉 Referral commission added!\nOrder: {order_id}\n"
                            f"Order amount: ₹{order['price']}\nCommission: ₹{int(order['price'] * COMMISSION_RATE)}\n"
                            f"Available balance: ₹{commission_balance(int(referrer_id))}")
                    except Exception as e:
                        print("Referrer notification failed:", e)
            save_json(ORDERS_FILE, orders)
            await context.bot.send_message(
                order["user_id"],
                f"🎉 Your Like Plan Successfully Activated!\n\n"
                f"✅ Plan: {order['plan']}\n"
                f"🎮 UID: {order.get('ff_uid', '-')}\n"
                f"🧾 Order ID: {order_id}\n"
                f"💳 Amount: ₹{order['price']}\n"
                f"📅 Plan Start Date: {start_date.strftime('%d-%m-%Y')}\n"
                f"⏳ Plan Expiry Date: {expiry_date.strftime('%d-%m-%Y')}\n\n"
                "Your likes will start from tomorrow morning. You will receive them right here in this bot's DM.\n\n"
                "Thank you for purchasing from FF Store!"
            )
            await query.edit_message_text(
                (query.message.text or "") +
                f"\n\n📅 Plan Start Date: {start_date.strftime('%d-%m-%Y')}"
                f"\n⏳ Plan Expiry Date: {expiry_date.strftime('%d-%m-%Y')}"
                "\n🏁 Status: COMPLETED"
            )
        return


async def order_lookup_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only lookup for a saved order by its Order ID."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if user.id != ADMIN_ID:
        await msg.reply_text("⛔ Access Denied! यह command सिर्फ Admin के लिए है।")
        return
    if not context.args:
        await msg.reply_text("इस्तेमाल: /order ORDER_ID\nउदाहरण: /order FFE4211513")
        return

    requested_id = context.args[0].strip().upper()
    order = None
    actual_id = None
    for key, item in orders.items():
        saved_id = str(item.get("order_id", key)).strip()
        if saved_id.upper() == requested_id or str(key).strip().upper() == requested_id:
            order = item
            actual_id = saved_id
            break

    if not order:
        await msg.reply_text(
            f"❌ Order नहीं मिला: {requested_id}\n"
            f"जाँचें कि सही Order ID दी है और वह {ORDERS_FILE} में मौजूद है।"
        )
        return

    created_at = order.get("created_at")
    if created_at:
        try:
            created_text = time.strftime("%d-%m-%Y %I:%M:%S %p", time.localtime(float(created_at)))
        except (TypeError, ValueError, OverflowError):
            created_text = str(created_at)
    else:
        created_text = "Not saved"

    order_status = str(order.get("status", "unknown"))
    status_label = {
        "pending_details": "Details Pending",
        "awaiting_uid": "Awaiting Free Fire UID",
        "uid_received": "UID Received",
        "awaiting_mobile": "Awaiting Mobile Number",
        "details_received": "Customer Details Received",
        "awaiting_payment": "Awaiting Payment",
        "payment_screenshot_received": "Payment Screenshot Received — Admin Verification Needed",
        "paid": "Payment Marked Paid",
        "completed": "Completed",
        "cancelled": "Cancelled",
        "rejected": "Rejected",
    }.get(order_status, order_status.replace("_", " ").title())

    user_id = order.get("user_id", "-")
    user_record = registered_users.get(str(user_id), {})
    username = order.get("username") or user_record.get("username") or ""
    name = order.get("name") or user_record.get("name") or "-"
    screenshot_saved = bool(order.get("payment_screenshot_file_id"))
    referrer_id = order.get("referrer_id")
    related_commissions = [
        item for item in referral_data.get("commissions", {}).values()
        if str(item.get("order_id", "")).upper() == requested_id
        or str(item.get("order_id", "")).upper() == str(actual_id).upper()
    ]
    commission_text = (
        "\n".join(
            f"₹{item.get('amount', 0)} — {str(item.get('status', 'unknown')).replace('_', ' ').title()} "
            f"(Referrer ID: {item.get('referrer_id', '-')})"
            for item in related_commissions
        )
        if related_commissions else "No commission record"
    )

    details = (
        "📦 FF STORE — ORDER DETAILS\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Order ID: {actual_id}\n"
        f"👤 Customer: {name}\n"
        f"🔗 Username: @{username if username else '-'}\n"
        f"🪪 Telegram User ID: {user_id}\n"
        f"🌍 Server Region: {order.get('server_region') or 'Not selected'}\n"
        f"🎮 Free Fire UID: {order.get('ff_uid') or 'Not submitted'}\n"
        f"📱 Mobile: {order.get('mobile') or 'Not submitted'}\n"
        f"💎 Plan: {order.get('plan', '-')}\n"
        f"💰 Amount: ₹{order.get('price', '-')}\n"
        f"📌 Status: {status_label}\n"
        f"💳 Screenshot saved: {'Yes' if screenshot_saved else 'No'}\n"
        f"📅 Created: {created_text}\n"
        f"👥 Referrer ID: {referrer_id if referrer_id else 'None'}\n"
        f"💸 Referral commission: {'Already recorded' if order.get('referral_commissioned') else 'Not marked as commissioned'}\n"
        f"🧮 Commission records:\n{commission_text}\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    await msg.reply_text(details)

async def send_admin_order(context, order_id):
    order = orders.get(order_id)
    if not order:
        return
    text = (
        "🛒 **New FF Store Order**\n"
        f"Order ID: `{order_id}`\n"
        f"Name: {order.get('name', '-')}\n"
        f"Username: @{order.get('username') or '-'}\n"
        f"User ID: `{order['user_id']}`\n"
        f"Plan: {order['plan']}\n"
        f"Amount: ₹{order['price']}\n"
        f"Server Region: {order.get('server_region', 'Not selected')}\n"
        f"Free Fire UID: {order.get('ff_uid', 'Not submitted')}\n"
        f"Mobile: {order.get('mobile', 'Not submitted')}\n"
        f"Status: {order['status']}"
    )
    try:
        await context.bot.send_message(ADMIN_ID, text, parse_mode="Markdown", reply_markup=admin_keyboard(order_id))
    except Exception as e:
        print("Admin notification failed:", e)

async def private_details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text or not update.effective_chat or update.effective_chat.type != "private":
        return
    if msg.text.startswith("/") or not context.user_data.get("awaiting"):
        return
    method = context.user_data.get("awaiting")
    value = msg.text.strip()

    if method == "withdrawal_upi":
        if not re.fullmatch(r"[A-Za-z0-9._-]{2,256}@[A-Za-z]{2,64}", value):
            await msg.reply_text("UPI ID सही format में भेजें, उदाहरण: name@upi")
            return
        user = update.effective_user
        balance = commission_balance(user.id)
        if balance < MIN_WITHDRAWAL:
            context.user_data.pop("awaiting", None)
            await msg.reply_text(f"आपका उपलब्ध balance अब ₹{balance} है। निकासी के लिए कम से कम ₹{MIN_WITHDRAWAL} चाहिए।")
            return
        existing = [w for w in referral_data["withdrawals"].values()
                    if int(w.get("user_id", 0)) == user.id and w.get("status") == "pending"]
        if existing:
            context.user_data.pop("awaiting", None)
            await msg.reply_text("आपकी एक withdrawal request पहले से pending है।")
            return
        commission_ids = [cid for cid, item in referral_data["commissions"].items()
                          if int(item.get("referrer_id", 0)) == user.id and item.get("status") == "available"]
        amount = sum(int(referral_data["commissions"][cid].get("amount", 0)) for cid in commission_ids)
        if amount < MIN_WITHDRAWAL:
            context.user_data.pop("awaiting", None)
            await msg.reply_text(f"उपलब्ध balance ₹{amount} है। निकासी के लिए ₹{MIN_WITHDRAWAL} चाहिए।")
            return
        withdrawal_id = "WD" + secrets.token_hex(4).upper()
        referral_data["withdrawals"][withdrawal_id] = {
            "withdrawal_id": withdrawal_id, "user_id": user.id, "amount": amount,
            "upi_id": value, "commission_ids": commission_ids,
            "status": "pending", "created_at": int(time.time())
        }
        for cid in commission_ids:
            referral_data["commissions"][cid]["status"] = "withdrawal_pending"
            referral_data["commissions"][cid]["withdrawal_id"] = withdrawal_id
        save_referral_data()
        context.user_data.pop("awaiting", None)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Mark UPI Paid", callback_data=f"withdraw:paid:{withdrawal_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"withdraw:reject:{withdrawal_id}")
        ]])
        await msg.reply_text(f"📤 Withdrawal request भेज दी गई।\nRequest: {withdrawal_id}\nAmount: ₹{amount}\nStatus: Admin verification pending.")
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"💸 Referral withdrawal request\nID: {withdrawal_id}\nUser ID: {user.id}\nAmount: ₹{amount}\nUPI ID: {value}",
                reply_markup=keyboard
            )
        except Exception as e:
            print("Withdrawal admin notification failed:", e)
            await msg.reply_text("Admin को सूचना नहीं भेजी जा सकी। कृपया admin से संपर्क करें; request record सुरक्षित है।")
        return

    if method == "wallet_topup_amount":
        if not value.isdigit() or not (30 <= int(value) <= 50000):
            await msg.reply_text("कृपया ₹30 से ₹50,000 के बीच पूरा रुपये में amount भेजें।")
            return
        context.user_data.pop("awaiting", None)
        # Reuse the same QR and admin-verification flow as fixed amount buttons.
        topup_id = "WT" + secrets.token_hex(4).upper()
        wallet_topups[topup_id] = {"topup_id": topup_id, "user_id": update.effective_user.id,
            "name": update.effective_user.full_name or "", "username": update.effective_user.username or "",
            "amount": int(value), "status": "pending", "created_at": int(time.time()), "payment_method": "phonepe_upi_qr"}
        save_wallet_topups(); context.user_data["current_topup"] = topup_id
        caption = (f"💰 FF STORE WALLET RECHARGE\nAmount: ₹{value}\nReference: {topup_id}\n\n"
                   "PhonePe में QR scan करके यही amount pay करें। Payment के बाद screenshot भेजें। Admin वास्तविक UPI receipt verify करके credit करेगा।")
        try:
            with open(QR_PATH, "rb") as qr:
                await context.bot.send_photo(chat_id=update.effective_user.id, photo=qr, caption=caption)
        except OSError:
            await msg.reply_text(caption + "\n⚠️ QR image file नहीं मिली।")
        return

    order_id = context.user_data.get("current_order")
    order = orders.get(order_id)
    if not order or order.get("user_id") != update.effective_user.id:
        context.user_data.pop("awaiting", None)
        return
    if method == "uid":
        if not re.fullmatch(r"\d{8,11}", value):
            await msg.reply_text("UID 8–11 अंकों का होना चाहिए। कृपया सही UID भेजें।")
            return
        order["ff_uid"] = value
        order["status"] = "uid_received"
        save_json(ORDERS_FILE, orders)
        context.user_data.pop("awaiting", None)
        await msg.reply_text(
            f"✅ UID verified and saved.\nUID: {value}\nOrder ID: {order_id}\n\nअब अपना 10 अंकों का mobile number submit करें:",
            reply_markup=method_keyboard(order_id, "mobile"),
        )
        # Admin approval is deferred until payment screenshot submission.
        return
    elif method == "mobile":
        if not re.fullmatch(r"[6-9]\d{9}", value):
            await msg.reply_text("कृपया 6, 7, 8 या 9 से शुरू होने वाला सही 10 अंकों का Indian mobile number भेजें।")
            return
        order["mobile"] = value
        order["status"] = "details_received"
    save_json(ORDERS_FILE, orders)
    context.user_data.pop("awaiting", None)
    await msg.reply_text(
        f"✅ Mobile number verified and saved.\nOrder ID: {order_id}\n"
        f"Plan: {order['plan']}\nAmount: ₹{order['price']}\n\n"
        "Payment method चुनें: Wallet से exact plan price कटेगा, या QR से payment कर सकते हैं।",
        reply_markup=payment_choice_keyboard(order_id),
    )
    # Admin approval is deferred until payment screenshot submission.

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_chat or update.effective_chat.type != "private":
        return
    if msg.photo and context.user_data.get("current_topup"):
        topup_id = context.user_data.get("current_topup")
        topup = wallet_topups.get(topup_id)
        if topup and int(topup.get("user_id", 0)) == update.effective_user.id and topup.get("status") == "pending":
            if topup.get("screenshot_file_id"):
                await msg.reply_text("इस recharge का screenshot पहले ही मिल चुका है। Admin verification का इंतज़ार करें।"); return
            topup["screenshot_file_id"] = msg.photo[-1].file_id
            topup["screenshot_at"] = int(time.time()); save_wallet_topups()
            await msg.reply_text("📩 Payment screenshot मिल गया। Admin आपके UPI में payment verify करने के बाद Wallet update करेगा।")
            kb = wallet_topup_keyboard(topup_id)
            await context.bot.send_photo(chat_id=ADMIN_ID, photo=msg.photo[-1].file_id,
                caption=(f"💰 WALLET RECHARGE REQUEST\nReference: {topup_id}\nUser: {topup.get('name')} (@{topup.get('username') or '-'})\n"
                         f"User ID: {topup['user_id']}\nAmount: ₹{topup['amount']}\nUPI: PhonePe QR\nStatus: Pending — verify actual bank credit before approval."),
                reply_markup=kb)
            return
    order_id = context.user_data.get("current_order")
    order = orders.get(order_id)
    if not order or order.get("user_id") != update.effective_user.id:
        return
    if msg.photo:
        if not order.get("ff_uid") or not order.get("mobile") or order.get("status") not in ("awaiting_payment", "payment_screenshot_received"):
            await msg.reply_text("पहले valid UID submit करें, फिर 10 अंकों का mobile number और Payment QR खोलें। उसके बाद ही payment screenshot भेजें।")
            return
        if order.get("status") == "payment_screenshot_received":
            await msg.reply_text("आपका screenshot पहले ही भेजा जा चुका है। Admin के निर्णय का इंतज़ार करें।")
            return
        order["payment_screenshot_file_id"] = msg.photo[-1].file_id
        order["status"] = "payment_screenshot_received"
        save_json(ORDERS_FILE, orders)
        await msg.reply_text("📩 Payment screenshot मिल गया। Admin verification के बाद status update होगा।")
        # Admin approval is deferred until payment screenshot submission.
        try:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=msg.photo[-1].file_id,
                caption=(
                    f"💳 Payment screenshot\nOrder ID: {order_id}\n"
                    f"User: {order.get('name', '-')} (@{order.get('username') or '-'})\n"
                    f"User ID: {order.get('user_id', '-')}\nPlan: {order['plan']}\n"
                    f"Amount: ₹{order['price']}\nServer Region: {order.get('server_region', 'Not selected')}\n"
                    f"UID: {order.get('ff_uid', '-')}\nMobile: {order.get('mobile', '-')}\nStatus: {order['status']}"
                ),
                reply_markup=admin_keyboard(order_id),
            )
        except Exception as e:
            print("Admin screenshot forwarding failed:", e)

async def group_command_redirect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Redirect every group command except /like to the private Like Assistant."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return
    await msg.reply_text(
        "❌ यह कमांड ग्रुप में इस्तेमाल नहीं की जा सकती।\n\n"
        "✅ इस कमांड का उपयोग करने के लिए हमारे Free Fire Like Assistant पर जाएँ।\n\n"
        "🤖 @FFStoreLikeBot"
    )
    # Stop later command handlers so they don't also reply in the group.
    raise ApplicationHandlerStop


async def plan_expiry_scheduler(application):
    """Send persistent daily reminders at 04:00 IST and one expiry notice."""
    while True:
        try:
            now = datetime.now(IST)
            today = now.date()
            # Run once the 04:00 IST point has passed. The sent-date ledger
            # prevents duplicates across restarts and repeated scheduler ticks.
            if now.hour >= 4:
                changed = False
                for user_key, plan in list(active_like_plans.items()):
                    try:
                        expiry = date.fromisoformat(plan["expiry_date"])
                        start = date.fromisoformat(plan["start_date"])
                        user_id = int(plan.get("user_id", user_key))
                    except (KeyError, ValueError, TypeError):
                        continue
                    sent = set(plan.get("reminder_sent_dates", []))
                    if start <= today <= expiry and today >= expiry - timedelta(days=2):
                        today_key = today.isoformat()
                        if today_key not in sent:
                            remaining = (expiry - today).days
                            if remaining == 0:
                                message = ("⏰ आपका Like Plan आज expire हो रहा है!\n"
                                           f"📦 Plan: {plan.get('plan', 'Like Plan')}\n"
                                           f"📅 Expiry Date: {expiry.strftime('%d-%m-%Y')}\n"
                                           "नया Like Plan खरीदने के लिए /like भेजें।")
                            else:
                                message = ("🔔 Like Plan Expiry Reminder\n"
                                           f"📦 Plan: {plan.get('plan', 'Like Plan')}\n"
                                           f"⏳ Expiry में {remaining} दिन बाकी हैं।\n"
                                           f"📅 Expiry Date: {expiry.strftime('%d-%m-%Y')}\n"
                                           "नया Plan खरीदने के लिए /like भेजें।")
                            try:
                                await application.bot.send_message(chat_id=user_id, text=message)
                                sent.add(today_key)
                                plan["reminder_sent_dates"] = sorted(sent)
                                changed = True
                            except Exception as exc:
                                print(f"Plan reminder failed for {user_id}: {exc}")
                    if today > expiry and not plan.get("expired_notice_sent", False):
                        try:
                            await application.bot.send_message(
                                chat_id=user_id,
                                text=("❌ आपका Like Plan expire हो चुका है।\n"
                                      f"📦 Plan: {plan.get('plan', 'Like Plan')}\n"
                                      f"📅 Expiry Date: {expiry.strftime('%d-%m-%Y')}\n\n"
                                      "कृपया नया Like Plan खरीदें। /like")
                            )
                            plan["expired_notice_sent"] = True
                            changed = True
                        except Exception as exc:
                            print(f"Plan expiry notice failed for {user_id}: {exc}")
                if changed:
                    save_active_like_plans()
        except Exception as exc:
            print("Plan expiry scheduler error:", exc)
        await asyncio.sleep(30)



# ---------------- FF STORE CUSTOMER APP API (shared in-process data) ----------------
try:
    from flask import Flask, jsonify, request, send_from_directory
except ImportError:
    Flask = None

app_login_codes = {}
app_sessions = {}
APP_SESSION_SECONDS = 7 * 24 * 60 * 60
APP_PORT = int(os.getenv("FFSTORE_APP_PORT", "5000"))
APP_WEB_DIR = Path(__file__).resolve().parent / "web"

async def appcode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.effective_chat or update.effective_chat.type != "private":
        return
    code = f"{secrets.randbelow(1000000):06d}"
    # Store only a digest; code expires in five minutes and is single-use.
    digest = hashlib.sha256(code.encode()).hexdigest()
    app_login_codes[digest] = {"user_id": user.id, "expires": time.time() + 300}
    await update.effective_message.reply_text(
        f"🔐 FF STORE App Login Code: {code}\n\nयह code 5 मिनट में expire होगा। इसे किसी से share न करें।"
    )

def start_customer_api():
    if Flask is None:
        print("FF STORE App API disabled: Flask package is not installed.")
        return None
    web_app = Flask("ffstore_customer_api", static_folder=None)
    def get_session():
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:].strip()
        item = app_sessions.get(hashlib.sha256(token.encode()).hexdigest())
        if not item or item["expires"] < time.time():
            return None
        return item
    def require_session():
        item = get_session()
        if not item:
            return None, (jsonify(error="Login expired. Telegram bot में /appcode भेजकर फिर login करें."), 401)
        return item, None
    @web_app.get("/")
    def home():
        return send_from_directory(str(APP_WEB_DIR), "index.html")
    @web_app.get("/<path:filename>")
    def static_files(filename):
        if filename.startswith("api/"):
            return jsonify(error="Not found"), 404
        return send_from_directory(str(APP_WEB_DIR), filename)
    @web_app.get("/api/health")
    def health():
        return jsonify(ok=True, service="FF STORE shared bot API", shared_process=True)
    @web_app.get("/api/plans")
    def api_plans():
        durations = {"p6":7,"p1":10,"p2":15,"p3":30,"p4":45,"p5":60}
        likes = {"p6":"1,540+","p1":"2,200+","p2":"3,300+","p3":"6,600+","p4":"9,900+","p5":"13,200+"}
        return jsonify([{"id":k,"name":v[0],"price":v[1],"duration":durations[k],"likes":likes[k]} for k,v in PLANS.items()])
    @web_app.post("/api/auth/verify")
    def auth_verify():
        body=request.get_json(silent=True) or {}; code=str(body.get("code", "")).strip()
        if not re.fullmatch(r"\d{6}", code): return jsonify(error="6 digit code डालें."),400
        digest=hashlib.sha256(code.encode()).hexdigest(); record=app_login_codes.pop(digest,None)
        if not record or record["expires"] < time.time(): return jsonify(error="Code गलत या expire हो गया. Bot में /appcode फिर भेजें."),401
        token=secrets.token_urlsafe(36); app_sessions[hashlib.sha256(token.encode()).hexdigest()]={"user_id":record["user_id"],"expires":time.time()+APP_SESSION_SECONDS}
        return jsonify(token=token)
    @web_app.get("/api/me")
    def api_me():
        session,error=require_session()
        if error:return error
        uid=int(session["user_id"]); user_orders=[o for o in orders.values() if int(o.get("user_id",0))==uid]
        return jsonify(user_id=uid,is_admin=uid==int(ADMIN_ID),wallet={"cash":wallet_balance(uid),"referral":commission_balance(uid),"available":wallet_available(uid)},orders=sorted(user_orders,key=lambda x:int(x.get("created_at",0)),reverse=True)[:30])
    @web_app.get("/api/payment-qr")
    def payment_qr():
        if not Path(QR_PATH).is_file(): return jsonify(error="Payment QR file missing"),404
        return send_from_directory(str(BOT_DIR), Path(QR_PATH).name)
    @web_app.get("/api/plan-qr/<plan_key>")
    def plan_payment_qr(plan_key):
        path=PLAN_QR_PATHS.get(plan_key)
        if not path or not Path(path).is_file(): return jsonify(error="Plan QR file missing"),404
        return send_from_directory(str(BOT_DIR), Path(path).name)
    @web_app.post("/api/topups")
    def create_topup():
        session,error=require_session()
        if error:return error
        uid=int(session["user_id"]); body=request.get_json(silent=True) or {}
        try: amount=int(body.get("amount",0))
        except (TypeError,ValueError): return jsonify(error="Valid amount डालें."),400
        if not 30 <= amount <= 50000:return jsonify(error="Recharge ₹30 से ₹50,000 के बीच होना चाहिए."),400
        topup_id="WT"+secrets.token_hex(4).upper()
        user=registered_users.get(str(uid),{})
        with wallet_lock:
            wallet_topups[topup_id]={"topup_id":topup_id,"user_id":uid,"name":user.get("name",""),"username":user.get("username",""),"amount":amount,"status":"pending","created_at":int(time.time()),"payment_method":"phonepe_upi_qr","source":"customer_app"}
            save_wallet_topups()
        return jsonify(topup_id=topup_id,amount=amount,status="pending",message="PhonePe QR scan करके payment करें. Screenshot Telegram bot में भेजें; admin manually verify करेगा.")
    @web_app.post("/api/orders")
    def create_order():
        session,error=require_session()
        if error:return error
        uid=int(session["user_id"]); body=request.get_json(silent=True) or {}; key=str(body.get("plan_key","")); region=str(body.get("region","IND")).upper(); ffuid=str(body.get("uid","")); mobile=str(body.get("mobile","")); method=str(body.get("payment_method","qr"))
        if key not in PLANS:return jsonify(error="Plan invalid"),400
        if region not in {x[1] for x in SERVER_REGIONS}:return jsonify(error="Server region invalid"),400
        if not re.fullmatch(r"\d{8,12}",ffuid):return jsonify(error="UID 8–12 digits का होना चाहिए."),400
        if not re.fullmatch(r"[6-9]\d{9}",mobile):return jsonify(error="सही 10 digit mobile number डालें."),400
        if method not in ("wallet","qr"):return jsonify(error="Payment method invalid"),400
        name,price=PLANS[key]; oid="FF"+secrets.token_hex(4).upper()
        order={"order_id":oid,"user_id":uid,"username":registered_users.get(str(uid),{}).get("username",""),"name":registered_users.get(str(uid),{}).get("name",""),"plan":name,"plan_key":key,"price":price,"status":"pending_details","created_at":int(time.time()),"referrer_id":referral_data.get("referrers",{}).get(str(uid)),"referral_commissioned":False,"server_region":region,"ff_uid":ffuid,"mobile":mobile,"source":"customer_app"}
        with wallet_lock:
            if method=="wallet":
                if wallet_available(uid)<price:return jsonify(error=f"Wallet balance कम है. Available ₹{wallet_available(uid)}; plan ₹{price}. पहले Add Money करें या QR payment चुनें."),400
                if not spend_wallet_funds(uid,price):return jsonify(error="Wallet debit failed; retry करें."),409
                order["wallet_debited"]=True;order["wallet_debited_at"]=int(time.time());order["payment_method"]="wallet";order["status"]="paid"
            else:
                order["payment_method"]="phonepe_upi_qr";order["status"]="awaiting_payment"
            orders[oid]=order;save_json(ORDERS_FILE,orders)
        if method=="wallet":
            if telegram_app and telegram_loop:
                asyncio.run_coroutine_threadsafe(telegram_app.bot.send_message(ADMIN_ID,f"🪙 APP WALLET ORDER PAID\nOrder: {oid}\nUser ID: {uid}\nPlan: {name}\nAmount: ₹{price}\nUID: {ffuid}\nRegion: {region}"),telegram_loop)
            return jsonify(order_id=oid,status="paid",amount=price,message="Wallet payment successful. Admin अब आपका plan activate करेगा.")
        return jsonify(order_id=oid,status="awaiting_payment",amount=price,qr=True,message=f"Order {oid} बन गया. ₹{price} का payment करके screenshot Telegram bot chat में भेजें और order ID बताएं.")
    def _forward_app_screenshot(file_storage, caption, keyboard):
        if not file_storage or not file_storage.filename:
            raise ValueError("Payment screenshot select करें.")
        if not (file_storage.mimetype or "").startswith("image/"):
            raise ValueError("केवल image screenshot स्वीकार है.")
        import tempfile
        suffix=Path(file_storage.filename).suffix[:10] or ".jpg"
        fd,tmp=tempfile.mkstemp(prefix="ffstore_app_",suffix=suffix,dir=str(BOT_DIR))
        os.close(fd)
        try:
            file_storage.save(tmp)
            if not telegram_app or not telegram_loop:
                raise RuntimeError("Telegram bot अभी connected नहीं है.")
            future=asyncio.run_coroutine_threadsafe(telegram_app.bot.send_photo(chat_id=ADMIN_ID,photo=open(tmp,"rb"),caption=caption,reply_markup=keyboard),telegram_loop)
            sent_message=future.result(timeout=45)
            return sent_message.photo[-1].file_id if sent_message.photo else None
        finally:
            try: os.remove(tmp)
            except OSError: pass
    @web_app.post("/api/topups/<topup_id>/screenshot")
    def topup_screenshot(topup_id):
        session,error=require_session()
        if error:return error
        uid=int(session["user_id"]); t=wallet_topups.get(topup_id)
        if not t or int(t.get("user_id",0))!=uid or t.get("status")!="pending":return jsonify(error="Top-up request नहीं मिला या बंद हो चुका है."),404
        if t.get("screenshot_file_id"):return jsonify(error="Screenshot पहले ही submit हो चुका है."),409
        try:
            fid=_forward_app_screenshot(request.files.get("screenshot"),f"💰 APP WALLET RECHARGE REQUEST\nReference: {topup_id}\nUser ID: {uid}\nAmount: ₹{t['amount']}\nPhonePe QR — verify actual bank credit before approval.",wallet_topup_keyboard(topup_id))
        except Exception as exc:return jsonify(error=str(exc)),400
        if not fid:return jsonify(error="Telegram screenshot forwarding failed."),502
        with wallet_lock:t["screenshot_file_id"]=fid;t["screenshot_at"]=int(time.time());t["screenshot_source"]="customer_app";save_wallet_topups()
        return jsonify(ok=True,message="Screenshot Admin को भेज दिया गया. UPI credit verify होने के बाद Wallet approve होगा.")
    @web_app.post("/api/orders/<order_id>/screenshot")
    def order_screenshot(order_id):
        session,error=require_session()
        if error:return error
        uid=int(session["user_id"]); o=orders.get(order_id)
        if not o or int(o.get("user_id",0))!=uid:return jsonify(error="Order नहीं मिला."),404
        if o.get("status") not in ("awaiting_payment","payment_screenshot_received"):return jsonify(error="इस order के लिए screenshot अभी स्वीकार नहीं है."),409
        if o.get("payment_screenshot_file_id"):return jsonify(error="Screenshot पहले ही submit हो चुका है."),409
        try:
            fid=_forward_app_screenshot(request.files.get("screenshot"),f"💳 APP PLAN PAYMENT SCREENSHOT\nOrder: {order_id}\nUser ID: {uid}\nPlan: {o['plan']}\nAmount: ₹{o['price']}\nRegion: {o.get('server_region')}\nUID: {o.get('ff_uid')}",admin_keyboard(order_id))
        except Exception as exc:return jsonify(error=str(exc)),400
        if not fid:return jsonify(error="Telegram screenshot forwarding failed."),502
        o["payment_screenshot_file_id"]=fid;o["status"]="payment_screenshot_received";o["screenshot_source"]="customer_app";o["screenshot_at"]=int(time.time());save_json(ORDERS_FILE,orders)
        return jsonify(ok=True,message="Screenshot Admin को भेज दिया गया. Verification के बाद order activate होगा.")

    @web_app.get("/api/admin/overview")
    def admin_overview():
        session,error=require_session()
        if error:return error
        if int(session["user_id"])!=int(ADMIN_ID):return jsonify(error="Admin access denied"),403
        pending_t=[{k:v for k,v in t.items() if k in ("topup_id","user_id","amount","status","screenshot_file_id","created_at")} for t in wallet_topups.values() if t.get("status")=="pending"]
        admin_orders=[{k:o.get(k) for k in ("order_id","user_id","plan","price","status","ff_uid","server_region","created_at")} for o in sorted(orders.values(),key=lambda x:int(x.get("created_at",0)),reverse=True)[:50]]
        return jsonify(users=len(registered_users),orders=len(orders),pending_topups=len(pending_t),topups=pending_t,orders_list=admin_orders)
    @web_app.post("/api/admin/topups/<topup_id>/<action>")
    def admin_topup_action(topup_id,action):
        session,error=require_session()
        if error:return error
        if int(session["user_id"])!=int(ADMIN_ID):return jsonify(error="Admin access denied"),403
        if action not in ("approve","reject"):return jsonify(error="Invalid action"),400
        with wallet_lock:
            t=wallet_topups.get(topup_id)
            if not t or t.get("status")!="pending":return jsonify(error="Top-up नहीं मिला या पहले process हो चुका है."),409
            if action=="approve":
                if not t.get("screenshot_file_id"):return jsonify(error="पहले screenshot Telegram bot में भेजें और UPI credit verify करें."),400
                target=int(t["user_id"]);wallets[str(target)]=wallet_balance(target)+int(t["amount"]);t["status"]="approved";t["approved_at"]=int(time.time());save_wallets();save_wallet_topups()
                textmsg=f"✅ Wallet top-up approved! Added ₹{t['amount']} • Reference {topup_id} • New balance ₹{wallet_available(target)}"
            else:
                target=int(t["user_id"]);t["status"]="rejected";t["rejected_at"]=int(time.time());save_wallet_topups();textmsg=f"❌ Wallet top-up {topup_id} reject हुई. Admin से संपर्क करें."
        if telegram_app and telegram_loop: asyncio.run_coroutine_threadsafe(telegram_app.bot.send_message(target,textmsg),telegram_loop)
        return jsonify(ok=True,message="Top-up "+action+" successful")
    @web_app.post("/api/admin/orders/<order_id>/<action>")
    def admin_order_action(order_id,action):
        session,error=require_session()
        if error:return error
        if int(session["user_id"])!=int(ADMIN_ID):return jsonify(error="Admin access denied"),403
        if action not in ("approve","reject"):return jsonify(error="Invalid action"),400
        order=orders.get(order_id)
        if not order:return jsonify(error="Order not found"),404
        uid=int(order["user_id"])
        if action=="reject":
            if order.get("status") in ("completed","rejected"):return jsonify(error="Order already processed"),409
            order["status"]="rejected";order["rejected_at"]=int(time.time());save_json(ORDERS_FILE,orders)
            if telegram_app and telegram_loop:asyncio.run_coroutine_threadsafe(telegram_app.bot.send_message(uid,f"❌ Order {order_id} reject हुआ. सहायता के लिए Admin से संपर्क करें."),telegram_loop)
            return jsonify(ok=True,message="Order rejected")
        if order.get("status") not in ("paid","payment_screenshot_received"):return jsonify(error="Payment status verify होने के बाद ही approve करें."),400
        if order.get("status")=="completed":return jsonify(error="Order already completed"),409
        today=datetime.now(IST).date(); start=today+timedelta(days=1); duration=plan_duration_days(order); expiry=start+timedelta(days=duration-1)
        order["status"]="completed";order["approved_at"]=int(time.time());order["plan_start_date"]=start.isoformat();order["plan_expiry_date"]=expiry.isoformat()
        active_like_plans[str(uid)]={"user_id":uid,"order_id":order_id,"plan":order.get("plan","Like Plan"),"start_date":start.isoformat(),"expiry_date":expiry.isoformat(),"reminder_sent_dates":[],"expired_notice_sent":False,"activated_at":int(time.time())};save_active_like_plans()
        if not order.get("referral_commissioned"):
            ref=order.get("referrer_id")
            if ref and int(ref)!=uid:
                cid=f"{order_id}_REF"
                if cid not in referral_data["commissions"]:referral_data["commissions"][cid]={"order_id":order_id,"referrer_id":int(ref),"buyer_id":uid,"amount":int(order["price"]*COMMISSION_RATE),"status":"available","created_at":int(time.time())}
                order["referral_commissioned"]=True;save_referral_data()
        save_json(ORDERS_FILE,orders)
        if telegram_app and telegram_loop:asyncio.run_coroutine_threadsafe(telegram_app.bot.send_message(uid,f"🎉 Like Plan Activated!\nPlan: {order['plan']}\nUID: {order.get('ff_uid','-')}\nOrder: {order_id}\nStart: {start.strftime('%d-%m-%Y')}\nExpiry: {expiry.strftime('%d-%m-%Y')}"),telegram_loop)
        return jsonify(ok=True,message=f"Plan activated. Start {start.isoformat()}, expiry {expiry.isoformat()}")
    server=ThreadingHTTPServer(("0.0.0.0",APP_PORT),web_app) if False else None
    # Flask's threaded development server is used only as an embedded local API listener.
    threading.Thread(target=lambda:web_app.run(host="0.0.0.0",port=APP_PORT,debug=False,use_reloader=False,threaded=True),daemon=True,name="ffstore-app-api").start()
    print(f"FF STORE Customer App API running on port {APP_PORT} (shared bot memory/database).")
    return web_app
# ---------------- END CUSTOMER APP API ----------------

async def main():
    global telegram_app, telegram_loop
    await client.start(phone=PHONE)
    application = Application.builder().token(BOT_TOKEN).build()
    telegram_app = application
    telegram_loop = asyncio.get_running_loop()
    start_customer_api()

    # Redirect group commands other than /like before normal command handlers run.
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.COMMAND
            & ~filters.Regex(r"(?i)^/like(?:@\w+)?(?:\s|$)"),
            group_command_redirect_handler,
        ),
        group=-1,
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            keyboard_button_handler,
        ),
        group=-1,
    )
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("appcode", appcode_handler))
    application.add_handler(CommandHandler("howtouse", howtouse_handler))
    application.add_handler(CommandHandler("admin", admin_handler))
    application.add_handler(CommandHandler("broadcast", broadcast_handler))
    application.add_handler(CommandHandler("referral", referral_handler))
    application.add_handler(CommandHandler("balance", balance_handler))
    application.add_handler(CommandHandler("withdraw", withdraw_handler))
    application.add_handler(CommandHandler("order", order_lookup_handler))
    # Group 0 handles purchase first; group 1 keeps the existing free-like flow intact.
    # In private chat, plain /like should open the purchase plans again.
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & filters.Regex(r"(?i)^/like(?:@\w+)?$"),
            purchase_handler,
        ),
        group=0,
    )
    # /like purchase continues to work in both private chat and group.
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"(?i)^/like(?:@\w+)?\s+purchase$"), purchase_handler),
        group=0,
    )
    application.add_handler(CallbackQueryHandler(broadcast_callback_handler, pattern=r"^broadcast:"), group=0)
    application.add_handler(CallbackQueryHandler(purchase_callback), group=0)
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, private_details_handler), group=1)
    application.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_handler), group=1)
    # Route only actual /like commands to the free-like handler.
    # The old broad filters.TEXT handler also matched ordinary messages in the
    # Like Purchase group, making its debug output misleading and obscuring
    # whether /like was received in the intended group.
    application.add_handler(
        MessageHandler(
            (filters.ChatType.GROUPS | filters.ChatType.PRIVATE)
            & filters.Regex(r"(?i)^/like(?:@\w+)?(?:$|\s+(?!purchase(?:\s|$)).*)"),
            like_handler,
        ),
        group=1,
    )

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    asyncio.create_task(plan_expiry_scheduler(application))
    print("Relay bot chal raha hai.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
