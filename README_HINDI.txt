FF STORE — WEB APP + ANDROID APK SOURCE PROJECT

इस पैकेज में:
1) FFStore_BOT_APP_INTEGRATED.py — Telegram Bot और उसी process में Customer API
2) web/ — Professional mobile Web App (custom Region/UID/Mobile/Payment modal)
3) android/ — Android Studio WebView app source project

महत्वपूर्ण:
- यह पैकेज APK binary नहीं है; Android SDK/Gradle build environment उपलब्ध न होने के कारण यहां APK compile नहीं किया गया।
- Android wrapper फिलहाल इसी फोन के localhost API (http://127.0.0.1:5000/) को खोलता है।
- APK तभी चलेगा जब उसी फोन पर Termux में integrated Bot/API चालू हो।
- Web App को उसी फोन पर Chrome में http://127.0.0.1:5000/ से खोलें।
- Bot folder में config.py, relay_before_purchase.py, Telethon session, JSON और QR files पहले की तरह रखें।
- Flask: pip install flask
- पुराने और नए Bot को एक साथ न चलाएं; एक ही Bot process रखें।
- Android APK बनाने के लिए Android Studio में android/ folder खोलें, Gradle sync करें और Build > Build APK(s) चलाएं।
- सार्वजनिक इंटरनेट Web App के लिए HTTPS hosting/tunnel और सुरक्षा hardening जरूरी है। Port 5000 को सीधे public internet पर expose न करें।
- PhonePe screenshot payments manual verification पर ही approve करें। पहले छोटे परीक्षण करें।


MOBILE-ONLY APK BUILD (GitHub Actions):
1) GitHub par naya repository banao aur is ZIP ka content upload karo (ZIP file ko repository me extract karke upload karna hai).
2) Repository ke Actions tab me "Build FF STORE APK" workflow select karke Run workflow dabao. Ya android/ files push karne par workflow auto run hoga.
3) Build complete hone par workflow run ke Artifacts section se FF-STORE-debug-APK download karo.
4) ZIP extract karke app-debug.apk ko Android phone par install karo. Debug APK signed for testing hota hai, Play Store release ke liye alag release signing chahiye.
5) Is app ka WebView http://127.0.0.1:5000/ kholta hai. Isliye APK chalate waqt isi phone par Termux me FFStore_BOT_APP_INTEGRATED.py chalna zaroori hai.
