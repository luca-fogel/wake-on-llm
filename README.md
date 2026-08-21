# qwen-control — turn a big local model on from anywhere

Two pieces that let you power a large local model on and off from your phone on
the other side of the world, over Tailscale. Built for a 72B; the model is a
config value (`LOCALAI_MODEL`), and this Mac now serves a 27B:

| Piece | Where | What it is |
|---|---|---|
| **QwenMenu.app** | Mac | menu bar icon — status at a glance, one click to toggle |
| **qwen-control** | Mac | LaunchAgent on the Mac's Tailscale IP, `:8121` — does the things only a local process can |
| **qwen-remote** | Pi | the page you actually open from abroad, `:8463` on the tailnet — **not in this repo** (see [The Pi side](#the-pi-side)) |

## Why the Pi is in the loop at all

Tailscale would let your phone reach the Mac directly — except a sleeping Mac
isn't on the tailnet at all, which is precisely when you want to turn something
on. The Pi is always on and shares the Mac's LAN, so it's the only box that can
truthfully report "the Mac is unreachable" and do something about it (a
best-effort Wake-on-LAN packet). It's the control surface; the Mac is the muscle.

## What "turn on" actually means here

This section has been wrong twice. The current version was settled on 2026-07-29
from the Mac's own LocalAI log and LaunchAgent plist, and the two earlier claims
are recorded below so nobody re-derives them.

### The actual mechanism

Two separate LocalAI features act on this model, and confusing them produced both
earlier mistakes. Both are set on the command line in
`~/Library/LaunchAgents/com.localai.server.plist` — not in a `.env`, and not in the
model YAML, which has no watchdog keys at all:

```
--enable-watchdog-idle --watchdog-idle-timeout=3m --enable-memory-reclaimer
```

**The memory reclaimer is the one that matters.** It evicts the least-recently-used
model when GPU memory use exceeds 95% of the Metal working-set budget. On this Mac
that budget is ~48 GiB (75% of 64 GiB, `iogpu.wired_limit_mb` unset) and the model
alone occupies ~47.8 GiB — about **99.7%**. The eviction condition is therefore
true the entire time the model is loaded. Logged `usage_percent` values: 99.79,
99.82, 99.80, against `threshold_percent=95`.

The reclaimer skips a model that has active API calls, so it fires the instant a
request finishes:

```
WARN [WatchDog] Memory usage exceeds threshold, evicting LRU backend type="GPU" usage_percent=99.79 threshold_percent=95
WARN [WatchDog] Skipping memory reclaimer eviction for busy model model="qwen2.5-72b" reason="model has active API calls"
INFO [WatchDog] Memory reclaimer evicting LRU model model="qwen2.5-72b"
```

That — not a per-request design — is what produced the "reaped ~5 s after every
response" behaviour. It is a memory-pressure symptom, and it will come back
whenever anything else wants GPU memory.

**The idle watchdog fires but does not work.** It triggers ~3 min after each
request, exactly as configured, and then fails to tear the backend down:

```
WARN [WatchDog] Address unresolvable address="127.0.0.1:56110"
```

`127.0.0.1:56110` was listening and serving throughout (`lsof` confirms it, and
requests to it succeeded). So idle reaping is currently broken, and **that broken
teardown is the only reason the model stays resident.** Residency here is an
accident, not a guarantee.

### Two corrections to earlier versions of this file

- ~~"LocalAI spawns the backend per request and reaps it seconds after the
  response."~~ Only true while the reclaimer is winning, i.e. under memory pressure.
- ~~"`[WatchDog] Address unresolvable` appears when a backend is reaped."~~ It means
  the opposite: the watchdog tried to reap and *failed*. The process survives.

Also worth knowing: **the Pi sends a chat completion every ~10 minutes**
(caught at 13:02:11 from the Pi's tailnet IP, and visible as an
unbroken ~602 s cadence in the log back to 2026-07-28 16:30). Any "the model
stayed resident for an hour with keep-warm off" observation on this system is
measuring that heartbeat, not idle residency. True no-traffic residency has still
never been measured, because nothing lets the Mac idle for more than ~10 minutes.

### Throughput

Fixed prompt, `max_tokens=64`, `temperature=0`, counting
`usage.completion_tokens` rather than assuming the requested tokens arrived.

| Condition | Wall time | Completion tokens | tok/s |
|---|---|---|---|
| warm (backend stably resident) | 17.4 s, 18.2 s, 18.4 s | 64 each | **~3.5** |
| cold (after `/backend/shutdown`) | 26.8 s, 27.2 s, 29.8 s | 64 each | ~2.2–2.4 |

Cold costs roughly **9–12 s of load** on top of generation, which is where the old
"2.3 tok/s" figure came from — it divided 38 tokens by a wall time that included
the load. There is no 2.3-vs-3.5 mystery: generation runs ~3.5 tok/s either way.

One caveat on method, because it is the whole point of hazard 3. After four loads
in quick succession the machine entered the reclaimer-evicting state described
above: `/state` began reporting `model_resident: false` while a backend pid still
existed, and a nominally "warm" run took 30.5 s because it was silently paying a
reload. Measurement was stopped there. The warm row above is the first batch only,
taken while the backend was stably resident; the contaminated run is excluded
rather than averaged in. Don't extend this table by brute force.

The earlier "1.05 s warm" figures were a 20-token *request* that almost certainly
returned only a couple of tokens; at 3.5 tok/s, 20 real tokens cannot arrive in
1.05 s. This is exactly why the table above reports `completion_tokens`.

~3.5 tok/s is unremarkable for a 44 GiB model on an M2 Max — memory-bandwidth
bound, and it is genuinely using the Metal backend (see below), so there is no
misconfiguration to recover here.

### It is on the Metal backend

Worth confirming because a CPU build would have explained the slow figures. It
didn't need explaining. The running process is
`data/backends/metal-llama-cpp/llama-cpp-cpu-all`, whose name is about bundled CPU
feature variants, not the compute backend — ggml loads its backend `.so` at
runtime, and the live process has `libggml-metal.so` and Apple's `AGXMetalG14X`
GPU driver mapped. All three registered backend names (`llama-cpp`,
`localai@llama-cpp`, `metal-llama-cpp`) resolve to the same Metal build; there is
no generic CPU llama-cpp installed.

### So what is keep-warm for now?

Less than it was built for. It is a hedge against the reclaimer winning a race,
not the load-bearing mechanism — and **no ping interval can prevent a reclaimer
eviction**, only reducing the model's footprint or disabling the reclaimer can.

It fires a 1-token completion every **120 s** for a bounded window (30 min by
default, 4 h ceiling). 120 s is chosen against the real
`--watchdog-idle-timeout=3m`: it has to stay inside 180 s to do anything, with
margin for a slow ping. The old 45 s interval was three times more often than
needed and spent power and heat for it.

The Pi page's `LOADED` (resident now) vs `WARM` (something is holding it up)
distinction is still correct and still worth keeping.

## Install

On the Mac, with the token from the Pi's `~/.config/qwen-remote/config.env`:

```bash
git clone https://github.com/foggylulu/qwen-control-mac.git
cd qwen-control-mac
QWEN_AGENT_TOKEN=<token> REMOTE_URL=https://pi.your-tailnet.ts.net:8463 ./install.sh
```

`REMOTE_URL` is optional and only adds the "open the remote page" menu item; it
is written to config rather than compiled in, because a tailnet hostname is
yours. `LOCALAI_MODEL=` overrides which model the agent operates on.

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
| `GET /state` | everything: server up, backend alive, model resident, keep-warm window, caffeinate, AC vs battery, GPU bytes, request counts, `caveats` |
| `POST /on` | caffeinate + start server if needed + keep-warm + warm once |
| `POST /off` | release keep-warm, unload the model, release caffeinate — **leaves the API server up** |
| `POST /warm` | one warm-up completion |
| `POST /unload` | `/backend/shutdown` the model now |
| `POST /keepwarm/on` `{"minutes":N}` / `/keepwarm/off` | bounded keep-warm window |
| `POST /caffeinate/on` / `/caffeinate/off` | hold / release the no-sleep assertion |
| `POST /localai/start` / `/localai/stop` | LaunchAgent control (see the warning) |

## The Pi side

Only the Mac half is published here. The Pi half is a small always-on web page
plus a poller, and it needs nothing from this repo beyond the HTTP contract in
the table above: hold the shared bearer token, `GET /state` to render, `POST /on`
and `POST /off` for the buttons, and treat a connection failure as "the Mac is
asleep" rather than an error — which is the one thing the Mac itself can never
report about itself.

If you are building your own, two things are worth copying and one is worth
avoiding:

- **Copy** the `LOADED` vs `WARM` distinction: resident right now, versus
  something is actively holding it up. They fail differently.
- **Copy** treating unreachable as a state, not a fault, with a best-effort
  Wake-on-LAN packet attached to it.
- **Avoid** polling `GET /v1/models` to answer "is the model there?". It lists
  *configured* models, so it answers "fine" whether or not any weights exist on
  disk. Verified the unhappy way: with the model files moved away, LocalAI still
  advertised the model and every completion returned HTTP 500. Poll `GET /state`
  on this agent, or `GET /system` on LocalAI.

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
- **On battery, both of the above get worse, and the agent now says so.** `/state`
  and the `POST /on` response carry a `caveats` array, and the menu bar shows
  *"On battery — sleeps on lid close"* above the action items. It is a warning, not
  a veto: nothing refuses to run on battery, because the person pressing the button
  may well know the lid is staying open.
- **Requests today is LocalAI's number, not ours.** The agent sits beside LocalAI,
  not in front of it, so it cannot see inbound traffic; it reads
  `api_call_count{path="/v1/chat/completions"}` from LocalAI's `/metrics` and stores
  a daily baseline, re-baselining when the counter goes backwards (a LocalAI
  restart). If `/metrics` can't be read the menu shows *unavailable* rather than a
  number. It previously showed a hardcoded-zero counter plus the agent's own
  warm-ups, which was wrong in both directions. First run after install undercounts
  the day, because the baseline can only start when the agent first looks.

## Open questions

The watchdog/model-YAML question is **answered** — see *The actual mechanism*. What
is left is narrower and needs a decision rather than an investigation.

**1. Should the memory reclaimer stay on?** — **ANSWERED: no. Removed
2026-08-18, and it was the whole problem.**

As configured it was structurally incompatible with the model on this machine:
47.8 GiB against a 45.6 GiB (95%) threshold means "evict" is always true, so
every request paid a fresh 9–12 s load. Dropping `--enable-memory-reclaimer` from
`com.localai.server.plist` took **warm time-to-first-byte from ~22 s to 0.7 s**,
a 30x difference, and turned residency from an accident of a broken watchdog into
the normal case. The idle watchdog was kept.

The tradeoff that made this look risky was the footprint: pinning ~47 GiB of a
64 GiB laptop leaves ~16 GiB for everything else. Both halves of that changed
together — the machine moved to a **27B at Q8_0 (~16 GiB)**, which is comfortably
under the threshold rather than permanently over it, so neither the reclaimer nor
a smaller `context_size` is needed to keep the machine usable.

The measurement above was taken on the 72B configuration on 2026-08-18. The
throughput table earlier in this file is also 72B-era; treat both as history for
that configuration rather than as current figures for whatever you run.

**2. Why is the idle watchdog's teardown broken?** `Address unresolvable` for a
port that is demonstrably listening suggests LocalAI is holding stale or
mis-keyed address bookkeeping for this backend — plausibly related to the
`localai@llama-cpp` → `metal-llama-cpp` meta-backend aliasing. This is inferred,
not established; it would need LocalAI's source to confirm. Low priority, since the
bug currently helps.

**3. What is the Pi's 10-minute heartbeat for?** It pins ~47 GiB resident around
the clock, including overnight on battery, which is a lot to spend on a liveness
check. The Pi also polls `GET /v1/models` every ~60 s, which the audit brief
already identified as useless for residency (it lists configured models, so it
always says "fine"). Both belong in the Pi repo, not this one, but a `GET /system`
poll would answer the residency question without loading anything.

## See also

Two other pieces of the same local-AI setup:

- [**mac-ai-activity**](https://github.com/foggylulu/mac-ai-activity) — what a
  generation actually costs: joules per token, per-slot llama.cpp telemetry, die
  temperatures.
- [**llmctl**](https://github.com/foggylulu/llmctl) — which engines are holding
  RAM, and one command to get it back.

## License

MIT — see [LICENSE](LICENSE). Contributions sign off with the
[DCO](CONTRIBUTING.md#sign-your-work-dco).
