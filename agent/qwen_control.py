#!/usr/bin/env python3
"""qwen-control: the Mac-local half of remote power control for qwen2.5-72b.

Runs as a macOS LaunchAgent bound *only* to this Mac's Tailscale IP. Two clients
talk to it: the menu bar app over loopback, and the Pi's qwen-remote page over
the tailnet. It exists because three of the things you need in order to "turn on"
a 72B model can only be done locally:

  * hold a no-sleep assertion, so the Mac stays reachable while it serves
  * start/stop LocalAI's LaunchAgent, when the model is not merely cold but dead
  * keep the llama-cpp backend resident, so requests stop paying cold-start

That last one used to be described here as "LocalAI reaps the backend after every
request, so every call pays ~8s of cold start". That was wrong, and the real
mechanism matters because it decides what keep-warm is for. Measured 2026-07-29
from this Mac's own logs:

  * LocalAI runs with `--enable-memory-reclaimer`, which evicts the least-recently
    used model whenever GPU memory use exceeds 95% of the Metal working-set budget.
    That budget is ~48 GiB (75% of 64 GiB) and this model alone occupies ~47.8 GiB,
    i.e. ~99.7% -- so the eviction condition is true the whole time the model is
    loaded. Logged usage_percent values: 99.79, 99.82, 99.80.
  * The reclaimer skips a model with active API calls, so in practice it fires the
    moment a request finishes. That -- not a per-request design -- produced the
    "reaped ~5s after every response" behaviour seen during a period of heavy load.
  * `--enable-watchdog-idle --watchdog-idle-timeout=3m` also fires, ~3m after each
    request, but currently *fails* to tear the backend down: it logs
    `[WatchDog] Address unresolvable address="127.0.0.1:NNNNN"` for a port that is
    demonstrably listening and serving. Idle reaping is therefore broken, and that
    broken teardown is the only reason the model currently stays resident.

So residency is presently accidental rather than guaranteed, and keep-warm is a
hedge against the reclaimer winning a race, not the load-bearing mechanism it was
built as. It stays bounded and expires on its own. Note that no ping interval can
prevent a reclaimer eviction -- only reducing the model's memory footprint or
disabling the reclaimer can, and both are LocalAI-side changes this agent will not
make on its own.

stdlib only; macOS ships a usable python3. No pip install.
"""
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONF_DIR = Path.home() / ".config" / "qwen-control"
CONF_FILE = CONF_DIR / "config.env"
STATE_FILE = CONF_DIR / "counters.json"

LOCALAI_LABEL = "com.localai.server"
LOCALAI_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LOCALAI_LABEL}.plist"

# The governing timer is LocalAI's --watchdog-idle-timeout=3m (read from the
# LaunchAgent plist, not guessed). Pinging must stay comfortably inside 180s or
# keep-warm does nothing; 45s was three times more often than needed and cost
# power and heat for it. 120s leaves a 60s margin for a slow ping.
KEEPWARM_PING_INTERVAL = 120
KEEPWARM_DEFAULT_MINUTES = 30    # bounded by default; never an open-ended burn
KEEPWARM_MAX_MINUTES = 240

# A cold load measured ~8s warm-cache on this Mac; 300s was arbitrary headroom.
# 180s is still ~20x the observed figure, which covers a load that has to fault
# 44 GiB back in from disk, without hanging a caller for five minutes.
WARM_TIMEOUT = 180

LOCALAI_CHAT_PATH = "/v1/chat/completions"


def _load_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


CFG = _load_env(CONF_FILE)
TOKEN = CFG.get("QWEN_AGENT_TOKEN", "")
BIND_HOST = CFG.get("QWEN_AGENT_BIND", "")          # this Mac's Tailscale IP
PORT = int(CFG.get("QWEN_AGENT_PORT", "8121"))
LOCALAI_BASE = CFG.get("LOCALAI_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
LOCALAI_KEY = CFG.get("LOCALAI_API_KEY", "")
MODEL = CFG.get("LOCALAI_MODEL", "qwen2.5-72b")
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", BIND_HOST.lower()} | {
    host.strip().lower() for host in CFG.get("QWEN_AGENT_ALLOWED_HOSTS", "").split(",") if host.strip()
}
ALLOWED_ORIGINS = {origin.strip() for origin in CFG.get("QWEN_AGENT_ALLOWED_ORIGINS", "").split(",") if origin.strip()}
MAX_BODY_BYTES = 4096
BODY_TIMEOUT_SECONDS = 5

if not TOKEN:
    raise SystemExit(f"refusing to start without QWEN_AGENT_TOKEN in {CONF_FILE}")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sh(*args, timeout=30):
    """Run a command, never raise. Returns (rc, stdout+stderr)."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return 255, str(exc)


def uid():
    return os.getuid()


def _localai(path, method="GET", body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{LOCALAI_BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if LOCALAI_KEY:
        req.add_header("Authorization", f"Bearer {LOCALAI_KEY}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return resp.status, (json.loads(raw) if raw.strip() else {})


def _localai_text(path, timeout=10):
    """Same as _localai but for endpoints that answer text, i.e. /metrics."""
    req = urllib.request.Request(f"{LOCALAI_BASE}{path}", method="GET")
    if LOCALAI_KEY:
        req.add_header("Authorization", f"Bearer {LOCALAI_KEY}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def chat_calls_total():
    """Cumulative count of POST /v1/chat/completions, from LocalAI's own metrics.

    This agent sits *beside* LocalAI rather than in front of it, so it cannot see
    inbound traffic itself -- it only knows about warm-ups it issued. LocalAI does
    know, and exposes it as an OpenTelemetry histogram on /metrics (which needs the
    API key). `api_call_count` is the histogram's count series: the same number as
    the le="+Inf" bucket, without the bucket label noise.

    Monotonic since the LocalAI process started, so it is a *cumulative* figure --
    turning it into "today" is requests_today's job. Returns None when metrics are
    unreachable or the series is absent, and callers surface that as unknown rather
    than as zero, because a confident 0 is exactly the bug this replaced.
    """
    try:
        _, text = _localai_text("/metrics", timeout=8)
    except Exception:
        return None
    for line in text.splitlines():
        if not line.startswith("api_call_count{"):
            continue
        labels, _, value = line.rpartition("}")
        if f'path="{LOCALAI_CHAT_PATH}"' in labels and 'method="POST"' in labels:
            try:
                return int(float(value.strip()))
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Counters, persisted so the menu bar figure survives an agent restart
# ---------------------------------------------------------------------------

# "requests" is derived from LocalAI's cumulative metric, not counted here; the
# extra keys are the bookkeeping that makes a monotonic counter answer "today".
#   chat_baseline -- cumulative value at the moment today's count started
#   chat_carried  -- requests already banked today, from earlier LocalAI processes
#   chat_last     -- last cumulative value seen, so a restart is detectable
_counters = {
    "day": "",
    "requests": 0,
    "warmups": 0,
    "chat_baseline": None,
    "chat_carried": 0,
    "chat_last": None,
}
_clock = threading.Lock()


def _roll_day_locked():
    """Reset the day's figures if the date changed. Caller holds _clock."""
    today = datetime.now().strftime("%Y-%m-%d")
    if _counters.get("day") != today:
        _counters.update({
            "day": today,
            "requests": 0,
            "warmups": 0,
            # Drop the baseline rather than zeroing it: the next sample re-baselines
            # against whatever LocalAI's cumulative counter reads at that point.
            "chat_baseline": None,
            "chat_carried": 0,
        })


def _load_counters():
    try:
        _counters.update(json.loads(STATE_FILE.read_text()))
    except Exception:
        pass
    with _clock:
        _roll_day_locked()


def _save_counters_locked():
    """Persist counters. Caller holds _clock.

    Written via a temporary file and os.replace so a crash mid-write cannot leave a
    truncated counters.json behind -- write_text truncates before it writes.
    """
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_counters))
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _bump(key):
    with _clock:
        _roll_day_locked()
        _counters[key] = _counters.get(key, 0) + 1
        _save_counters_locked()


def requests_today(total):
    """Turn LocalAI's cumulative chat-completion count into a figure for today.

    Two things break a naive delta and both are handled here: the day rolling over,
    and LocalAI restarting. A restart resets its counter to zero, which is visible
    as the cumulative value going *backwards* -- when that happens we bank the span
    we had already accounted for and re-baseline at zero, so requests served by the
    previous process are neither lost nor double counted.

    Returns None if `total` is None, which propagates "unknown" all the way to the
    menu bar instead of showing a figure that isn't true.
    """
    if total is None:
        return None
    with _clock:
        _roll_day_locked()
        base = _counters.get("chat_baseline")
        last = _counters.get("chat_last")
        if base is None or last is None:
            base = total
            _counters["chat_baseline"] = base
        elif total < last:
            _counters["chat_carried"] = _counters.get("chat_carried", 0) + max(0, last - base)
            base = 0
            _counters["chat_baseline"] = base
        _counters["chat_last"] = total
        today = _counters.get("chat_carried", 0) + max(0, total - base)
        _counters["requests"] = today
        _save_counters_locked()
        return today


# ---------------------------------------------------------------------------
# caffeinate -- hold the Mac awake
# ---------------------------------------------------------------------------
# A MacBook on battery will still sleep on lid close no matter what assertion we
# hold; -s only reliably prevents system sleep on AC. The honest contract here is
# "don't idle-sleep while plugged in", not "never sleep".

_caffeinate = {"proc": None, "since": None}


def caffeinate_on():
    if _caffeinate["proc"] and _caffeinate["proc"].poll() is None:
        return True, "already held"
    try:
        p = subprocess.Popen(
            ["caffeinate", "-i", "-m", "-s"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _caffeinate["proc"] = p
        _caffeinate["since"] = time.time()
        return True, "holding"
    except Exception as exc:
        return False, str(exc)


def caffeinate_off():
    p = _caffeinate["proc"]
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _caffeinate["proc"] = None
    _caffeinate["since"] = None
    return True, "released"


def caffeinate_held():
    p = _caffeinate["proc"]
    return bool(p and p.poll() is None)


def on_ac_power():
    rc, out = sh("pmset", "-g", "batt", timeout=10)
    if rc != 0:
        return None
    return "AC Power" in out


def power_caveats(ac=None):
    """Honest warnings about what the awake/warm machinery can't deliver right now.

    Reported rather than enforced. The agent does not refuse an action on battery --
    the person asking may well know something the code doesn't, e.g. that the lid is
    staying open and the model is wanted now. But they should not have to already
    know this to find it out.

    `ac` may be passed in by a caller that has already probed pmset, so /state does
    not shell out twice for the same answer.
    """
    if ac is None:
        ac = on_ac_power()
    if ac is False:
        return [
            "On battery: this Mac will still sleep when the lid closes, whatever "
            "assertion is held -- caffeinate -s only reliably prevents idle sleep "
            "on AC. If it sleeps it drops off the tailnet and cannot be woken "
            "remotely (Wake-on-LAN has never succeeded on this machine).",
            "On battery, keep-warm spends power and heat holding ~47 GiB resident "
            "for a latency win of roughly 8s per request. Plug in first if the "
            "window matters.",
        ]
    return []


# ---------------------------------------------------------------------------
# One action at a time
# ---------------------------------------------------------------------------
# Two overlapping /on calls both triggering a 44 GiB load is how the 2026-07-29
# thrashing incident started: the machine went to 718ms LAN latency, Tailscale fell
# back to a relay and reported offline, and it eventually needed physical attention.
# ThreadingHTTPServer means a slow /on doesn't block /state, which is what we want,
# but it also means nothing stops a second /on arriving mid-load. So: mutating
# actions take this lock and a caller who cannot get it is told so, rather than
# queued -- a queued second load is the thing we are trying to avoid.

_action_lock = threading.Lock()


# ---------------------------------------------------------------------------
# LocalAI LaunchAgent control
# ---------------------------------------------------------------------------
# Deliberately conservative. A plist edit followed by a bare `kickstart` once left
# this service fully unloaded and took hours of blind remote recovery, so: never
# touch the plist, and treat bootout as the only thing that can strand us --
# always pair it with an explicit bootstrap, and verify.

def localai_loaded():
    rc, _ = sh("launchctl", "print", f"gui/{uid()}/{LOCALAI_LABEL}", timeout=15)
    return rc == 0


def localai_start():
    if localai_loaded():
        rc, out = sh("launchctl", "kickstart", f"gui/{uid()}/{LOCALAI_LABEL}", timeout=60)
        return rc == 0, f"kickstart rc={rc} {out.strip()[:200]}"
    if not LOCALAI_PLIST.exists():
        return False, f"missing plist {LOCALAI_PLIST}"
    rc, out = sh("launchctl", "bootstrap", f"gui/{uid()}", str(LOCALAI_PLIST), timeout=60)
    if rc != 0:
        # Older/racier path; harmless if bootstrap already half-succeeded.
        rc2, out2 = sh("launchctl", "load", "-w", str(LOCALAI_PLIST), timeout=60)
        return rc2 == 0 or localai_loaded(), f"bootstrap rc={rc}; load rc={rc2} {out2.strip()[:160]}"
    return True, "bootstrapped"


def localai_stop():
    rc, out = sh("launchctl", "bootout", f"gui/{uid()}/{LOCALAI_LABEL}", timeout=60)
    ok = rc == 0 or not localai_loaded()
    return ok, f"bootout rc={rc} {out.strip()[:200]}"


def backend_pids():
    # Match the gRPC backend LocalAI actually spawns -- it is always started with an
    # --addr flag, e.g.
    #   data/backends/metal-llama-cpp/llama-cpp-cpu-all --addr 127.0.0.1:56110
    # A bare "llama-cpp" pattern also matched llama-cpp-rpc-server and any shell
    # that merely mentioned the string, which would report a backend that isn't one.
    rc, out = sh("pgrep", "-f", r"llama-cpp.*--addr", timeout=10)
    if rc != 0:
        return []
    return [x for x in out.split() if x.isdigit()]


def localai_api_ok():
    try:
        _, data = _localai("/system", timeout=6)
        loaded = [m.get("id") for m in data.get("loaded_models", []) if isinstance(m, dict)]
        return True, loaded
    except Exception:
        return False, []


def gpu_bytes():
    rc, out = sh("ioreg", "-r", "-d", "1", "-c", "IOAccelerator", timeout=15)
    if rc != 0:
        return None
    for line in out.splitlines():
        if "In use system memory" in line:
            digits = "".join(ch for ch in line.split("=")[-1] if ch.isdigit())
            if digits:
                return int(digits)
    return None


# ---------------------------------------------------------------------------
# keep-warm
# ---------------------------------------------------------------------------

_keepwarm = {"until": 0.0, "last_ping": None, "last_error": None}


def warm_once(timeout=WARM_TIMEOUT):
    """One tiny completion. Loading the model *is* a request."""
    _localai(
        "/v1/chat/completions",
        method="POST",
        body={
            "model": MODEL,
            "messages": [{"role": "user", "content": "ok"}],
            "max_tokens": 1,
            "temperature": 0,
        },
        timeout=timeout,
    )
    _bump("warmups")


def keepwarm_on(minutes=None):
    m = int(minutes or KEEPWARM_DEFAULT_MINUTES)
    m = max(1, min(KEEPWARM_MAX_MINUTES, m))
    _keepwarm["until"] = time.time() + m * 60
    return True, f"keeping warm for {m} min"


def keepwarm_off():
    _keepwarm["until"] = 0.0
    return True, "keep-warm released"


def keepwarm_active():
    return time.time() < _keepwarm["until"]


def keepwarm_loop():
    while True:
        try:
            if keepwarm_active():
                # Take the same lock the HTTP actions take, without blocking: if an
                # /on is already loading, this tick has nothing useful to add and
                # would only stack a second request behind a 44 GiB load.
                if _action_lock.acquire(blocking=False):
                    try:
                        warm_once()
                        _keepwarm["last_ping"] = now_iso()
                        _keepwarm["last_error"] = None
                    except Exception as exc:
                        _keepwarm["last_error"] = f"{type(exc).__name__}: {exc}"
                    finally:
                        _action_lock.release()
                else:
                    _keepwarm["last_error"] = "skipped: another action was running"
        except Exception:
            pass
        time.sleep(KEEPWARM_PING_INTERVAL)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# /state is polled by the menu bar every 5s and by the Pi page as well, and each
# call used to shell out to launchctl, pmset and ioreg every time. Those three
# answers change on the order of seconds at best, so a short TTL removes most of the
# subprocess churn without making the display meaningfully staler.
_probe_cache = {}
_probe_lock = threading.Lock()
PROBE_TTL = 4.0


def _cached(key, fn):
    now = time.time()
    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit and now - hit[0] < PROBE_TTL:
            return hit[1]
    value = fn()
    with _probe_lock:
        _probe_cache[key] = (now, value)
    return value


def build_state():
    loaded_ok, loaded = localai_api_ok()
    pids = backend_pids()
    ac = _cached("ac", on_ac_power)
    requests = requests_today(_cached("chat_total", chat_calls_total))
    with _clock:
        counters = dict(_counters)
    counters["requests"] = requests          # None when metrics are unreachable
    counters["requests_source"] = "localai_metrics" if requests is not None else "unavailable"
    return {
        "at": now_iso(),
        "model": MODEL,
        "localai_loaded": _cached("launchd", localai_loaded),
        "localai_api": loaded_ok,
        "model_resident": MODEL in loaded,
        "loaded_models": loaded,
        "backend_pids": pids,
        "backend_alive": bool(pids),
        "caffeinate_held": caffeinate_held(),
        "caffeinate_since": _caffeinate["since"],
        "on_ac_power": ac,
        "caveats": power_caveats(ac),
        "keep_warm": {
            "active": keepwarm_active(),
            "until": _keepwarm["until"] or None,
            "seconds_left": max(0, int(_keepwarm["until"] - time.time())) if keepwarm_active() else 0,
            "last_ping": _keepwarm["last_ping"],
            "last_error": _keepwarm["last_error"],
            "interval": KEEPWARM_PING_INTERVAL,
        },
        "gpu_bytes": _cached("gpu", gpu_bytes),
        "counters": counters,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_POST_ROUTES = {
    "/caffeinate/on", "/caffeinate/off",
    "/keepwarm/on", "/keepwarm/off",
    "/localai/start", "/localai/stop",
    "/warm", "/unload", "/on", "/off",
}


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that doesn't dump a traceback when a client vanishes.

    Clients here vanish routinely and harmlessly: the menu bar app is restarted on
    every upgrade, the Pi's connection drops whenever the Mac sleeps or Tailscale
    re-routes. The default handler prints a full ConnectionResetError traceback to
    stderr for each one, which fills agent.err with noise that looks like a fault
    and buries anything that actually is one.
    """

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "qwen-control"
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(BODY_TIMEOUT_SECONDS)

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _authed(self):
        # A loopback address identifies the machine, not the user or application.
        # The menu bar app reads the same owner-only config as this agent.
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        # compare_digest, not ==, so the comparison time carries no information
        # about how many leading bytes were right. It refuses str with non-ASCII,
        # and this header is attacker-controlled, so compare the bytes instead --
        # latin-1 is the encoding http.server already decoded the header from.
        return bool(TOKEN) and hmac.compare_digest(
            got.encode("latin-1", "replace"), TOKEN.encode("latin-1", "replace")
        )

    def _trusted_request(self):
        try:
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                return False
            host = urlsplit("//" + hosts[0])
            if (host.hostname not in ALLOWED_HOSTS or host.username is not None
                    or host.password is not None or host.path or host.query or host.fragment
                    or (host.port or 80) != self.server.server_port):
                return False
            origins = self.headers.get_all("Origin", [])
            return not origins or (len(origins) == 1 and origins[0] != "null" and origins[0] in ALLOWED_ORIGINS)
        except ValueError:
            return False

    def _reject(self, status, message):
        # Never reuse a connection whose body has not been consumed.
        self.close_connection = True
        self._json(status, {"error": message})

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("transfer encoding is unsupported")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise ValueError("one valid Content-Length is required")
        n = int(lengths[0])
        if not 0 < n <= MAX_BODY_BYTES:
            raise ValueError("request body must contain 1 to 4096 bytes")
        deadline = time.monotonic() + BODY_TIMEOUT_SECONDS
        data = bytearray()
        while len(data) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request body timed out")
            self.connection.settimeout(remaining)
            chunk = self.rfile.read1(n - len(data))
            if not chunk:
                raise ValueError("incomplete request body")
            data.extend(chunk)
        body = json.loads(data.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        self.connection.settimeout(BODY_TIMEOUT_SECONDS)
        return body

    def do_GET(self):
        if not self._trusted_request():
            self._reject(403, "untrusted Host or Origin")
            return
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/health":
            self._json(200, {"status": "ok", "service": "qwen-control"})
            return
        if not self._authed():
            self._reject(401, "unauthorized")
            return
        if path in ("/", "/state"):
            self._json(200, build_state())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._trusted_request():
            self._reject(403, "untrusted Host or Origin")
            return
        if not self._authed():
            self._reject(401, "unauthorized")
            return
        path = self.path.split("?")[0].rstrip("/")
        if path not in _POST_ROUTES:
            self._reject(404, "not found")
            return
        try:
            body = self._body()
        except (TimeoutError, socket.timeout):
            self._reject(408, "request body timed out")
            return
        except (ValueError, UnicodeDecodeError):
            self._reject(400, "invalid JSON object or request framing")
            return

        # Every mutating route is serialised. Refuse rather than queue: a second
        # caller waiting behind a 44 GiB load will simply load it again when its turn
        # comes, which is the failure we are guarding against.
        if not _action_lock.acquire(blocking=False):
            self._json(409, {
                "ok": False,
                "detail": "another action is already running; try again shortly",
                "state": build_state(),
            })
            return
        try:
            ok, detail, extra = self._dispatch(path, body)
        finally:
            _action_lock.release()

        payload = {"ok": ok, "detail": detail, "state": build_state()}
        payload.update(extra)
        self._json(200 if ok else 500, payload)

    def _dispatch(self, path, body):
        """Run one mutating action. Returns (ok, detail, extra_response_fields)."""
        extra = {}

        if path == "/caffeinate/on":
            ok, detail = caffeinate_on()
        elif path == "/caffeinate/off":
            ok, detail = caffeinate_off()
        elif path == "/keepwarm/on":
            ok, detail = keepwarm_on(body.get("minutes"))
        elif path == "/keepwarm/off":
            ok, detail = keepwarm_off()
        elif path == "/localai/start":
            ok, detail = localai_start()
        elif path == "/localai/stop":
            ok, detail = localai_stop()
        elif path == "/warm":
            try:
                warm_once()
                ok, detail = True, "warmed"
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
        elif path == "/unload":
            try:
                _localai("/backend/shutdown", method="POST", body={"model": MODEL}, timeout=60)
                ok, detail = True, "unloaded"
            except Exception as exc:
                # LocalAI answers 500 "model not found" when the model is not loaded.
                # For an unload request that is the desired end state, not a failure,
                # so it counts as success -- any other error does not.
                msg = str(exc)
                ok, detail = ("model not found" in msg), msg
        elif path == "/on":
            # Composite: awake + server up + model warm, held for a bounded window.
            caffeinate_on()
            if not localai_api_ok()[0]:
                localai_start()
                for _ in range(20):
                    time.sleep(3)
                    if localai_api_ok()[0]:
                        break
            keepwarm_on(body.get("minutes"))
            # Surfaced in the response so the Pi page can show it next to the button
            # that was just pressed, which is the only place it is any use.
            extra["caveats"] = power_caveats()
            try:
                warm_once()
                ok, detail = True, "on"
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
        elif path == "/off":
            # Release the expensive things; leave the API server up. Stopping
            # LocalAI itself is a separate, explicit call -- an idle API server
            # costs nothing, and bootout is the one move that can strand us.
            keepwarm_off()
            try:
                _localai("/backend/shutdown", method="POST", body={"model": MODEL}, timeout=60)
            except Exception:
                pass
            caffeinate_off()
            ok, detail = True, "off"

        return ok, detail, extra


def main():
    _load_counters()
    threading.Thread(target=keepwarm_loop, daemon=True).start()

    # Bind loopback for the menu bar and the explicit Tailscale address for the
    # remote client. Both listeners require the same bearer token.
    servers = []
    hosts = ["127.0.0.1"]
    if BIND_HOST and BIND_HOST != "127.0.0.1":
        hosts.append(BIND_HOST)

    for host in hosts:
        try:
            srv = QuietThreadingHTTPServer((host, PORT), Handler)
            servers.append((host, srv))
        except OSError as exc:
            print(f"could not bind {host}:{PORT} -- {exc}", flush=True)

    if not servers:
        raise SystemExit(f"no listener could bind port {PORT}")

    for host, srv in servers[:-1]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"qwen-control on http://{host}:{PORT}", flush=True)

    host, srv = servers[-1]
    print(f"qwen-control on http://{host}:{PORT} (model {MODEL})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
