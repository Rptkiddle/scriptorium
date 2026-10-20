#!/bin/zsh
# Build and install Scriptorium.app into ~/Applications.
# Rebuilding changes the code signature, which VOIDS the existing TCC
# grant — after any rebuild, launch the app once from Finder to re-grant.
set -e
cd "$(dirname "$0")"
APP="$HOME/Applications/Scriptorium.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cc -O2 -o "$APP/Contents/MacOS/Scriptorium" stub.c
cp Info.plist "$APP/Contents/Info.plist"
codesign --force -s - "$APP"
echo "installed: $APP"
echo "next: open it once from Finder to trigger the Desktop-folder grant"
