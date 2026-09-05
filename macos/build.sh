#!/bin/zsh
# Build + sign SlyTermBar.app with a STABLE identity so TCC grants
# (mic, Pictures, Music, Messages, ...) survive updates.
# Ad-hoc signing = new identity every build = macOS re-prompts every update.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/SlyTermBar.app"
IDENTITY="Apple Development: pelotonysl@outlook.com (9Q38C6TT37)"
VERSION="0.5.0"
BUILD="5"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -o "$APP/Contents/MacOS/SlyTermBar" "$DIR/SlyTermBar.swift" \
  -framework AppKit -framework WebKit -framework ServiceManagement

cp "$DIR/logo.png" "$APP/Contents/Resources/logo.png"
[[ -f "$DIR/AppIcon.icns" ]] && cp "$DIR/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>SlyTermBar</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleIdentifier</key><string>com.sly.slytermbar</string>
  <key>CFBundleName</key><string>SlyTermBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>__VERSION__</string>
  <key>CFBundleVersion</key><string>__BUILD__</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>SlyTermBar opens Terminal windows to run slyterm.</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>Voice input for Claude in the menu bar terminal.</string>
  <key>NSSpeechRecognitionUsageDescription</key>
  <string>Dictation into the menu bar terminal.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST
sed -i '' -e "s/__VERSION__/$VERSION/" -e "s/__BUILD__/$BUILD/" "$APP/Contents/Info.plist"

codesign --force --deep --sign "$IDENTITY" "$APP"
codesign -dv "$APP" 2>&1 | grep -E 'TeamIdentifier|Authority' || true
echo "built $APP v$VERSION ($BUILD)"
