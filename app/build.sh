#!/bin/zsh
# Build and install Scriptorium.app into ~/Applications, and install the
# daemon's and menubar's LaunchAgents into ~/Library/LaunchAgents (from the
# templates in launchd/, with this user's home folder filled in).
# Rebuilding changes the code signature, which VOIDS the existing TCC
# grant — after any rebuild, launch the app once from Finder to re-grant.
set -e
cd "$(dirname "$0")"
REPO="${PWD:h}"
APP="$HOME/Applications/Scriptorium.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cc -O2 -DDISPATCH="\"$REPO/app/dispatch.sh\"" -o "$APP/Contents/MacOS/Scriptorium" stub.c
cp Info.plist "$APP/Contents/Info.plist"
codesign --force -s - "$APP"
echo "installed: $APP"

AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$HOME/Library/Logs/scriptorium"
for name in scriptorium scriptorium-menubar; do
  target="$AGENTS/io.github.rptkiddle.$name.plist"
  sed "s|__HOME__|$HOME|g" "$REPO/launchd/$name.plist" > "$target"
  echo "installed: $target"
done
echo "next: open the app once from Finder (Desktop-folder grant), then load each agent:"
echo "  launchctl bootstrap gui/\$UID ~/Library/LaunchAgents/io.github.rptkiddle.scriptorium.plist"
echo "  launchctl bootstrap gui/\$UID ~/Library/LaunchAgents/io.github.rptkiddle.scriptorium-menubar.plist"
