# SafeRoute Android app (VIVO Y55S, Android 6.0.1)

This is a small Android app that opens the SafeRoute server in its own full-screen window.
It works on Android 6.0 (API 23) and newer.

## Before you build

1. Start the SafeRoute server and make sure its public link works in a browser.
2. If the link changed, update `saferoute_url` in `app/src/main/res/values/strings.xml`.

## Build the APK (on your PC)

1. Install [Android Studio](https://developer.android.com/studio) (it includes Java).
2. Open the `android` folder in Android Studio (File > Open). Let it finish syncing. It creates the Gradle wrapper automatically.
3. Build > Build Bundle(s) / APK(s) > Build APK(s).
4. The APK is at `app/build/outputs/apk/debug/app-debug.apk`.

## Install on the VIVO Y55S

1. Copy `app-debug.apk` to the phone (USB, or send it to yourself).
2. Open it in the file manager and allow installs from that source when asked.
3. Open SafeRoute and allow location when asked, so "Use my location" works.

## Things to check on an older phone

- **Web view version.** The app shows the page with the phone's Android System WebView. Update it from the Play Store (search "Android System WebView"). An old WebView can fail to run the page.
- **Mobile data.** The phone needs internet to reach the server. Without it, the app shows the "cannot reach the server" message.
- **Location.** Turn on GPS, or the location may take a long time or fail.
