# Trinity

A tiny Android app that keeps a local Wi-Fi HTTP receiver available for MP3 files.

## What it does

- One Android setup screen starts/stops an always-on foreground receiver on port `1234`.
- The receiver can restart after phone reboot after it has been enabled once.
- Uploads require the pairing token shown in the Android app.
- Laptop browser can open `http://PHONE_IP:1234/` and choose MP3 files.
- The updated `laptop/find_lan_upload.py` can also upload directly to `POST /upload-file?filename=...` without Playwright/browser clicking.
- Files are saved to `Music/TrinityUploads` on the Android device.

## Build APK

Open this folder in Android Studio, then use:

```text
Build > Build Bundle(s) / APK(s) > Build APK(s)
```

Or from a terminal with Android Gradle Plugin support installed:

```bash
gradle assembleDebug
```

The debug APK will be created at:

```text
app/build/outputs/apk/debug/app-debug.apk
```

## Use

1. Install the APK on Android.
2. Open Trinity once, allow notifications/storage prompts, and allow battery optimization exemption if Android asks.
3. Copy the pairing token shown in the app the first time the laptop asks for it.
4. After that, keep the phone and laptop on the same Wi-Fi and run the normal laptop flow.
5. Trinity keeps a foreground notification while the receiver is active.
