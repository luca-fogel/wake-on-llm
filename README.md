# qwen-control — turn the Mac's Qwen 2.5 72B on from anywhere

Two pieces that let you power a 72B local model on and off from your phone on the
other side of the world, over Tailscale:

| Piece | Where | What it is |
|---|---|---|
| **QwenMenu.app** | Mac | menu bar icon — status at a glance, one click to toggle |
| **qwen-control** | Mac | LaunchAgent on the Mac's Tailscale IP, `:8121` — does the things only a local process can |
| **qwen-remote** | Pi | the page you actually open from abroad, `:8463` on the tailnet (lives in the Pi repo) |

## Why the Pi is in the loop at all

Tailscale would let your phone reach the Mac directly — except a sleeping Mac
isn't on the tailnet at all, which is precisely when you want to turn something
on. The Pi is always on and shares the Mac's LAN, so it's the only box that can
truthfully report "the Mac is unreachable" and do something about it (a
best-effort Wake-on-LAN packet). It's the control surface; the Mac is the muscle.

## What "turn on" actually means here

Worth being blunt, because the naive mental model is wrong on this machine.

LocalAI **spawns the llama-cpp backend per request and reaps it seconds after the
response.** Verified on 2026-07-29, and the Pi's own dashboard shows the backend
process has read `down` since 2026-07-28 22:22. So:

- a request arriving **after** the backend was reaped pays roughly **8 s of cold start**
- a request arriving **while it's still alive** is fast — measured 1.60 s then
  1.05 s for a 20-token completion, back to back, on the Mac itself

That gap is the entire point of this repo: latency depends on whether you happen
to catch the backend alive, which is why the Pi page reports `LOADED` (resident
right now, lapsing) separately from `WARM` (something is holding it up).

Cold-case throughput measured ~2.3 tok/s (38 tokens in 16.8 s, including the
load). The warm case looked far better but wasn't measured with a controlled
token count — treat warm tok/s as unestablished rather than quoting a number.

### ⚠️ But how often the backend is actually reaped is unresolved

Stated plainly because it changes what this repo is for. The per-request reaping
above was measured in a ~25 minute window on 2026-07-29 — `loaded_models` empty
across ten checks over 68 s, reproduced after a full LocalAI restart. Hours later
the model was **resident with keep-warm off and the last ping over an hour
earlier**, and stayed resident across six polls over three minutes with no
inference traffic at all.

The likely explanation is that the reaping was a *symptom of memory pressure* —
that window is exactly when repeated 47 GB loads drove the machine into thrashing
(LAN ping 10 ms → 718 ms, Tailscale offline, eventually needing physical
attention). If that's right then **keep-warm is mostly unnecessary** and the 8 s
cold start is not the normal case. Not yet established either way; settling it is
the first task in the audit brief, and needs the Mac's own logs.

There's no external config that changes this, so `qwen-control` keeps the backend
alive the blunt way: **keep-warm** fires a 1-token completion every 45 s for a
bounded window (30 min by default, 4 h ceiling). It trades idle power and heat
for latency, deliberately, and expires on its own so it can't be left burning.

If you'd rather fix it properly at the source, the thing to investigate on the
Mac is LocalAI's watchdog (`WATCHDOG_IDLE`, `WATCHDOG_IDLE_TIMEOUT`,
`single_active_backend`) and the model YAML — see *Open question* below.

## Install

On the Mac, with the token from the Pi's `~/.config/qwen-remote/config.env`:

```bash
git clone https://github.com/foggylulu/qwen-control-mac.git
cd qwen-control-mac
QWEN_AGENT_TOKEN=<token> ./install.sh
```

Idempotent — re-run to upgrade. Needs Xcode Command Line Tools for the menu bar
app (`xcode-select --install`); without them the agent still installs and the Pi
page still works, you just don't get the icon.

The installer auto-detects the Mac's Tailscale IP and tries to find the LocalAI
API key in the existing LocalAI plist / bridge env. Override either with
`QWEN_AGENT_BIND=` / `LOCALAI_API_KEY=`.

## API

`GET /health` is open (no auth). Everything else needs
`Authorization: Bearer <token>` — except from `127.0.0.1`, which is trusted so the
menu bar app doesn't need to hold a secret.

| Route | Effect |
|---|---|
| `GET /state` | everything: server up, backend alive, model resident, keep-warm window, caffeinate, AC vs battery, GPU bytes, request counts |
| `POST /on` | caffeinate + start server if needed + keep-warm + warm once |
| `POST /off` | release keep-warm, unload the model, release caffeinate — **leaves the API server up** |
| `POST /warm` | one warm-up completion |
| `POST /unload` | `/backend/shutdown` the model now |
| `POST /keepwarm/on` `{"minutes":N}` / `/keepwarm/off` | bounded keep-warm window |
| `POST /caffeinate/on` / `/caffeinate/off` | hold / release the no-sleep assertion |
| `POST /localai/start` / `/localai/stop` | LaunchAgent control (see the warning) |

## Two safety decisions worth knowing

**`/off` does not stop the LocalAI server.** It unloads the model and drops the
assertions; the idle API server costs essentially nothing. Stopping the server is
`launchctl bootout`, and if that's the last thing you do remotely then *nothing
on the tailnet can start it again* — you'd need to be at the Mac. So it's a
separate call, behind a confirm dialog in the menu bar app, and never what the
power button does.

**The installer never edits `com.localai.server.plist`.** Editing it and
restarting with a bare `kickstart` once left LocalAI fully unloaded and took hours
of blind remote recovery. Every service this repo does own is installed
bootout → bootstrap → verify, never kickstart-on-unloaded.

## Caveats, honestly

- **Wake-on-LAN is best-effort.** MacBooks only honour network wake on AC power,
  and Wi-Fi wake needs a Bonjour sleep-proxy router. This Mac is on Wi-Fi with a
  *randomized* MAC that rotates, so the Pi re-reads it from ARP whenever the Mac
  is visible and persists the last-seen value — the address you need is only
  observable while the machine is awake, i.e. never when you need it. If wake
  fails, someone has to open the lid.
- **`caffeinate` is not absolute.** A MacBook on battery will still sleep on lid
  close. The real contract is "don't idle-sleep while plugged in."
- **Keep-warm costs power and heat** on an M2 Max holding a 72B model. That's why
  it's bounded and self-expiring rather than a plain on/off switch.

## Open question

Whether LocalAI can be made to hold the backend resident properly, instead of
being kept alive by a ping. Worth an hour with the LocalAI watchdog settings and
the `qwen2.5-72b` model YAML on the Mac. If it can, keep-warm becomes a fallback
rather than the mechanism, and the ~8 s cold start and 2.3 tok/s both improve.
