#!/bin/zsh
# Scriptorium.app dispatch: all modes the launcher stub can run.
# Invoked as: Scriptorium.app/Contents/MacOS/Scriptorium <mode>
#   daemon    the MCP daemon (LaunchAgent com.rupertkiddle.scriptorium)
#   menubar   the menubar companion (…-menubar)
#   surveil   one nightly surveillance cycle (…-surveillance)
#   (none)    interactive launch: trigger/verify the Desktop-folder grant
# Children inherit the app's TCC identity, so every file touch below is
# attributed to "Scriptorium" in Privacy & Security.

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
REPO="$HOME/Desktop/code/1_SELF/scriptorium/scriptorium"

# Activity Monitor names a process after the binary it executes, and the
# venv's python3 resolves to uv's python3.13. The daemon and menubar
# therefore run from an APFS clone of that interpreter named after their
# role (.venv/proc/<name>); Python finds the venv through
# .venv/pyvenv.cfg one level up. The clone is remade at every launch, so
# it follows interpreter upgrades and venv rebuilds, and it is swapped in
# by rename, so a running process is never touched. If cloning fails, the
# caller falls back to plain `uv run`.
named_python() {
  local dir="$REPO/.venv/proc"
  local target="$dir/$1"
  mkdir -p "$dir" || return 1
  cp -c "$(readlink -f "$REPO/.venv/bin/python3")" "$target.tmp.$$" \
    && mv -f "$target.tmp.$$" "$target" \
    && print -r -- "$target"
}

case "$1" in
  daemon)
    cd "$REPO" || exit 1
    py=$(named_python scriptorium) && exec "$py" .venv/bin/scriptorium serve
    exec uv run --no-sync scriptorium serve ;;
  menubar)
    cd "$REPO" || exit 1
    py=$(named_python scriptorium-menubar) && exec "$py" .venv/bin/scriptorium-menubar
    exec uv run --no-sync scriptorium-menubar ;;
  surveil)
    # One nightly surveillance cycle, driven by code (scriptorium.launcher):
    # Claude is called only for the filter and appraise judgements, without
    # tools. Output goes to the log; the run's record stays in its folder.
    cd "$REPO" || exit 1
    LOG_DIR="$HOME/Library/Logs/scriptorium"
    mkdir -p "$LOG_DIR"
    exec "$REPO/.venv/bin/python" -m scriptorium.launcher >> "$LOG_DIR/surveillance.log" 2>&1 ;;
  *)
    # Finder launch: the ls triggers macOS's one-time Desktop-folder
    # prompt, attributed to Scriptorium; the dialog reports the outcome.
    if ls "$HOME/Desktop" >/dev/null 2>&1; then result="granted ✓"; else result="DENIED ✗"; fi
    osascript -e "display dialog \"Desktop folder access: $result\" buttons {\"OK\"} default button 1 with title \"Scriptorium\"" >/dev/null 2>&1 ;;
esac
