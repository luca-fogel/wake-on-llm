#!/bin/bash
# Install qwen-control (agent) + QwenMenu.app (menu bar) on this Mac.
#
#   QWEN_AGENT_TOKEN=<token from the Pi> ./install.sh
#
# Idempotent: safe to re-run to upgrade. Never touches com.localai.server.plist --
# a past edit to that file left LocalAI unloaded and took hours to recover.
set -euo pipefail

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UID_NUM="$(id -u)"
LA="$HOME/Library/LaunchAgents"
AGENT_DIR="$HOME/qwen-control"
CONF_DIR="$HOME/.config/qwen-control"
APP_DIR="$HOME/Applications/QwenMenu.app"

AGENT_LABEL="com.foggylulu.qwen-control"
MENU_LABEL="com.foggylulu.qwen-menu"

[[ "$(uname -s)" == "Darwin" ]] || die "this installer is for macOS"

# ---------------------------------------------------------------- token
TOKEN="${QWEN_AGENT_TOKEN:-${1:-}}"
if [[ -z "$TOKEN" && -f "$CONF_DIR/config.env" ]]; then
  TOKEN="$(sed -n 's/^QWEN_AGENT_TOKEN=//p' "$CONF_DIR/config.env" | head -1)"
  [[ -n "$TOKEN" ]] && say "reusing the token already in $CONF_DIR/config.env"
fi
[[ -n "$TOKEN" ]] || die "need QWEN_AGENT_TOKEN (get it from the Pi: ~/.config/qwen-remote/config.env)"

# ---------------------------------------------------------------- tailscale IP
TS_IP="${QWEN_AGENT_BIND:-}"
if [[ -z "$TS_IP" ]]; then
  for ts in /Applications/Tailscale.app/Contents/MacOS/Tailscale \
            /usr/local/bin/tailscale /opt/homebrew/bin/tailscale tailscale; do
    if command -v "$ts" >/dev/null 2>&1 || [[ -x "$ts" ]]; then
      TS_IP="$("$ts" ip -4 2>/dev/null | head -1 || true)"
      [[ -n "$TS_IP" ]] && break
    fi
  done
fi
if [[ -z "$TS_IP" ]]; then
  TS_IP="$(ifconfig 2>/dev/null | awk '/^utun/{u=1} u && /inet 100\./{print $2; exit}')"
fi
[[ -n "$TS_IP" ]] || die "could not determine this Mac's Tailscale IP -- pass QWEN_AGENT_BIND=100.x.y.z"
say "binding the agent to $TS_IP:8121 (tailnet only, never 0.0.0.0)"

# ---------------------------------------------------------------- LocalAI creds
LOCALAI_MODEL="${LOCALAI_MODEL:-qwen2.5-72b}"
KEY="${LOCALAI_API_KEY:-}"
PLIST="$LA/com.localai.server.plist"
if [[ -z "$KEY" && -f "$PLIST" ]]; then
  # Read-only introspection of the LocalAI plist: pull an api-keys flag or env var
  # if one is set there. We never write to this file.
  KEY="$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:LOCALAI_API_KEY' "$PLIST" 2>/dev/null || true)"
  if [[ -z "$KEY" ]]; then
    KEY="$(plutil -convert xml1 -o - "$PLIST" 2>/dev/null \
           | sed -n 's/.*--api-keys[= ]\([^<[:space:]]*\).*/\1/p' | head -1 || true)"
  fi
fi
if [[ -z "$KEY" && -f "$HOME/local-bridge/bridge.env" ]]; then
  KEY="$(sed -n 's/^LOCALAI_API_KEY=//p' "$HOME/local-bridge/bridge.env" | head -1 || true)"
fi
[[ -n "$KEY" ]] || warn "no LocalAI API key found; if LocalAI requires one, add LOCALAI_API_KEY= to $CONF_DIR/config.env and restart the agent"

# ---------------------------------------------------------------- config
say "writing $CONF_DIR/config.env"
mkdir -p "$CONF_DIR"
umask 077
cat > "$CONF_DIR/config.env" <<EOF
# qwen-control (Mac) -- written by install.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)
QWEN_AGENT_TOKEN=$TOKEN
QWEN_AGENT_BIND=$TS_IP
QWEN_AGENT_PORT=8121
LOCALAI_BASE_URL=http://127.0.0.1:8080
LOCALAI_API_KEY=$KEY
LOCALAI_MODEL=$LOCALAI_MODEL
EOF
chmod 600 "$CONF_DIR/config.env"

# ---------------------------------------------------------------- agent
say "installing the agent to $AGENT_DIR"
mkdir -p "$AGENT_DIR"
cp "$SRC/agent/qwen_control.py" "$AGENT_DIR/qwen_control.py"
chmod 755 "$AGENT_DIR/qwen_control.py"

PY="$(command -v python3 || echo /usr/bin/python3)"
mkdir -p "$LA"
cat > "$LA/$AGENT_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$AGENT_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$AGENT_DIR/qwen_control.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$AGENT_DIR/agent.log</string>
  <key>StandardErrorPath</key><string>$AGENT_DIR/agent.err</string>
  <key>ProcessType</key><string>Background</string>
</dict></plist>
EOF

# bootout-then-bootstrap, and verify -- a bare kickstart on an unloaded service
# is the failure mode that caused the long outage on this machine.
launchctl bootout "gui/$UID_NUM/$AGENT_LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$UID_NUM" "$LA/$AGENT_LABEL.plist" 2>/dev/null \
  || launchctl load -w "$LA/$AGENT_LABEL.plist" 2>/dev/null || true
sleep 2
launchctl print "gui/$UID_NUM/$AGENT_LABEL" >/dev/null 2>&1 \
  && say "agent service loaded" \
  || warn "agent service did not load; try: launchctl load -w $LA/$AGENT_LABEL.plist"

# ---------------------------------------------------------------- menu bar app
if command -v swiftc >/dev/null 2>&1; then
  say "building QwenMenu.app"
  rm -rf "$APP_DIR"
  mkdir -p "$APP_DIR/Contents/MacOS"
  cat > "$APP_DIR/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>QwenMenu</string>
  <key>CFBundleDisplayName</key><string>Qwen</string>
  <key>CFBundleIdentifier</key><string>com.foggylulu.qwen-menu</string>
  <key>CFBundleExecutable</key><string>QwenMenu</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF
  swiftc -O -parse-as-library=false \
    -target "$(uname -m)-apple-macos12.0" \
    -framework AppKit \
    -o "$APP_DIR/Contents/MacOS/QwenMenu" \
    "$SRC/menubar/main.swift" \
    || die "swiftc failed -- fix the compile errors above and re-run"
  say "built $APP_DIR"

  cat > "$LA/$MENU_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$MENU_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$APP_DIR/Contents/MacOS/QwenMenu</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict></plist>
EOF
  launchctl bootout "gui/$UID_NUM/$MENU_LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$LA/$MENU_LABEL.plist" 2>/dev/null \
    || launchctl load -w "$LA/$MENU_LABEL.plist" 2>/dev/null || true
  open "$APP_DIR" 2>/dev/null || true
  say "QwenMenu launched and set to start at login"
else
  warn "swiftc not found -- skipping the menu bar app."
  warn "Install Xcode Command Line Tools (xcode-select --install) and re-run to get it."
fi

# ---------------------------------------------------------------- verify
say "verifying"
sleep 2
if curl -fsS --max-time 6 "http://127.0.0.1:8121/health" >/dev/null 2>&1; then
  say "agent answering on loopback"
else
  warn "agent not answering yet -- check $AGENT_DIR/agent.err"
fi
if curl -fsS --max-time 8 -H "Authorization: Bearer $TOKEN" "http://$TS_IP:8121/state" >/dev/null 2>&1; then
  say "agent answering on the tailnet at $TS_IP:8121"
else
  warn "agent not answering on $TS_IP -- is Tailscale up?"
fi

cat <<EOF

Done.

  Menu bar        look for the flame / moon icon near the clock
  Agent           http://$TS_IP:8121  (tailnet only, bearer token)
  Remote control  https://pi.your-tailnet.ts.net:8463  (from the Pi)

On the Pi, confirm the two sides agree:
  curl -s http://127.0.0.1:8150/api/state | python3 -m json.tool | grep -A3 agent

Uninstall:
  launchctl bootout gui/$UID_NUM/$AGENT_LABEL
  launchctl bootout gui/$UID_NUM/$MENU_LABEL
  rm -rf "$AGENT_DIR" "$APP_DIR" "$LA/$AGENT_LABEL.plist" "$LA/$MENU_LABEL.plist" "$CONF_DIR"
EOF
