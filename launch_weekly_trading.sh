#!/bin/zsh
# One-shot launcher for weekly_trading_spy.py, invoked by the
# com.dansavage.weekly-spy-launch LaunchAgent on Mon 8/31/26 09:31 ET.
# Runs the trader detached (survives terminal/session close) and then
# unloads its own LaunchAgent so it never fires again.

set -euo pipefail

REPO_DIR="/Users/danielsavage/Desktop/Folders/VisualStudio/trade-iron-butterfly-weekly"
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"

export ALPACA_CLI_PROFILE="spy_dansavage_pa3em2vld7xn"

cd "$REPO_DIR"
LOGFILE="$LOG_DIR/weekly_trading_spy_$(date +%Y%m%d_%H%M%S).log"

nohup /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
    weekly_trading_spy.py < /dev/null >> "$LOGFILE" 2>&1 &
disown

# Self-remove the LaunchAgent so this one-off doesn't recur next year.
launchctl bootout "gui/$(id -u)" \
    "$HOME/Library/LaunchAgents/com.dansavage.weekly-spy-launch.plist" 2>/dev/null || true
