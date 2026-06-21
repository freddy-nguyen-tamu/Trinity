# Trinity

A tiny Android app that hosts a local Wi-Fi HTTP receiver for MP3 files.

## What it does

- One main Android button starts/stops a server on port `1234`.
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
2. Connect phone and laptop to the same Wi-Fi.
3. Open the app and tap **Start LAN Server**.
4. On the laptop run `python find_lan_upload.py` from the `laptop` folder, or open the URL shown in the app.
5. Keep the app open while uploading.
