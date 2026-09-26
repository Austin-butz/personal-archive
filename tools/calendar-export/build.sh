#!/bin/sh
# Builds build/CalendarExport.app (ad-hoc signed).
# macOS ties the Calendar permission to this exact build: rebuilding means
# re-approving the prompt, so only rebuild when main.swift changes.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
app="$here/../../build/CalendarExport.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp "$here/Info.plist" "$app/Contents/Info.plist"
swiftc -O "$here/main.swift" -o "$app/Contents/MacOS/CalendarExport"
codesign --force --sign - "$app"
echo "built $app"
