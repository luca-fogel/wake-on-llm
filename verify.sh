#!/bin/bash
# Post-install self-check. Prints a report; changes nothing.
#
# Exists so verifying an install never requires pasting credentials or curl auth
# headers around: everything it needs it reads from the config the installer
# already wrote.
set -uo pipefail

CONF="$HOME/.config/qwen-control/config.env"
ok()   { printf '  \033[1;32mPASS\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; }
info() { printf '  ....  %s\n' "$*"; }

get() { sed -n "s/^$1=//p" "$CONF" 2>/dev/null | head -1; }

echo
echo "qwen-control self-check"
echo "======================="

if [[ ! -f "$CONF" ]]; then
  bad "no config at $CONF -- run ./install.sh first"
  exit 1
fi
ok "config present"

TOKEN="$(get QWEN_AGENT_TOKEN)"
BIND="$(get QWEN_AGENT_BIND)"
PORT="$(get QWEN_AGENT_PORT)"
KEY="$(get LOCALAI_API_KEY)"
MODEL="$(get LOCALAI_MODEL)"
BASE="$(get LOCALAI_BASE_URL)"
PORT="${PORT:-8121}"

# 1. token sanity -- length and shape only, never printed
if [[ ${#TOKEN} -eq 48 ]]; then
  ok "shared token looks right (48 chars)"
elif [[ -z "$TOKEN" ]]; then
  bad "no shared token in config"
else
  bad "shared token is ${#TOKEN} chars, expected 48 -- probably a placeholder, so the Pi will not pair"
fi

# 2. loopback listener (what the menu bar app uses)
if curl -fsS --max-time 6 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  ok "agent answering on loopback :$PORT"
else
  bad "agent NOT answering on loopback :$PORT -- the menu bar app cannot work"
fi

# 3. tailscale listener (what the Pi uses)
if [[ -n "$BIND" && "$BIND" != "127.0.0.1" ]]; then
  if curl -fsS --max-time 8 -H "Authorization: Bearer $TOKEN" \
       "http://$BIND:$PORT/state" >/dev/null 2>&1; then
    ok "agent answering on the tailnet at $BIND:$PORT"
  else
    bad "agent NOT answering on $BIND:$PORT -- is Tailscale up?"
  fi
else
  bad "no tailnet bind address configured"
fi

# 4. launchd services
for label in com.foggylulu.qwen-control com.foggylulu.qwen-menu; do
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    ok "$label loaded"
  else
    bad "$label not loaded"
  fi
done

# 5. menu bar process
if pgrep -x QwenMenu >/dev/null 2>&1; then
  ok "QwenMenu process running (look for a flame or moon icon by the clock)"
else
  bad "QwenMenu not running"
fi

# 6. model residency, and a cold-start timing only if genuinely cold
echo
echo "model state"
echo "-----------"
SYS="$(curl -fsS --max-time 8 -H "Authorization: Bearer $KEY" "${BASE%/}/system" 2>/dev/null)"
if [[ -z "$SYS" ]]; then
  bad "LocalAI not answering at ${BASE%/}/system"
  exit 0
fi
RESIDENT="$(printf '%s' "$SYS" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("loaded_models") or []))' 2>/dev/null || echo "?")"
if [[ "$RESIDENT" == "0" ]]; then
  info "backend is cold -- timing one 20-token completion (this is the number worth having)"
  T0=$(python3 -c 'import time;print(time.time())')
  curl -fsS --max-time 300 -X POST \
    -H "Content-Type: application/json" -H "Authorization: Bearer $KEY" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"say hi\"}],\"max_tokens\":20}" \
    "${BASE%/}/v1/chat/completions" >/dev/null 2>&1
  T1=$(python3 -c 'import time;print(time.time())')
  printf '  ....  cold start + 20 tokens: %.2fs\n' "$(python3 -c "print($T1-$T0)")"
else
  info "backend is already warm ($RESIDENT model(s) resident) -- skipping the timing,"
  info "a warm number tells us nothing new. Re-run in a few minutes if you want the cold figure."
fi
echo
