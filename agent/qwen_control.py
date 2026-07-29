#!/usr/bin/env python3
"""qwen-control: the Mac-local half of remote power control for qwen2.5-72b.

Runs as a macOS LaunchAgent bound *only* to this Mac's Tailscale IP. Two clients
talk to it: the menu bar app over loopback, and the Pi's qwen-remote page over
the tailnet. It exists because three of the things you need in order to "turn on"
a 72B model can only be done locally:

  * hold a no-sleep assertion, so the Mac stays reachable while it serves
  * start/stop LocalAI's LaunchAgent, when the model is not merely cold but dead
  * keep the llama-cpp backend resident, so requests stop paying cold-start

That last one is the real problem on this machine. LocalAI here spawns the
backend per request and reaps it a few seconds after the response, so every call
pays ~8s of load and runs ~2.3 tok/s. There is no config that fixes this from
outside, so the agent keeps the backend alive the blunt way: a tiny completion on
a timer, for a bounded window. That is what "keep warm" means below -- it trades
idle power for latency, on purpose, and expires on its own so it can never be
left burning forever.

stdlib only; macOS ships a usable python3. No pip install.
"""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONF_DIR = Path.home() / ".config" / "qwen-control"
CONF_FILE = CONF_DIR / "config.env"
STATE_FILE = CONF_DIR / "counters.json"

LOCALAI_LABEL = "com.localai.server"
LOCALAI_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LOCALAI_LABEL}.plist"

KEEPWARM_PING_INTERVAL = 45      # comfortably inside the observed ~5s..reap window
KEEPWARM_DEFAULT_MINUTES = 30    # bounded by default; never an open-ended burn
KEEPWARM_MAX_MINUTES = 240


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


# ---------------------------------------------------------------------------
# Counters, persisted so the menu bar figure survives an agent restart
# ---------------------------------------------------------------------------

_counters = {"day": "", "requests": 0, "warmups": 0}
_clock = threading.Lock()


def _load_counters():
    global _counters
    try:
        _counters.update(json.loads(STATE_FILE.read_text()))
    except Exception:
        pass
    today = datetime.now().strftime("%Y-%m-%d")
    if _counters.get("day") != today:
        _counters.update({"day": today, "requests": 0, "warmups": 0})


def _save_counters():
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(_counters))
    except Exception:
        pass


def _bump(key):
    with _clock:
        today = datetime.now().strftime("%Y-%m-%d")
        if _counters.get("day") != today:
            _counters.update({"day": today, "requests": 0, "warmups": 0})
        _counters[key] = _counters.get(key, 0) + 1
        _save_counters()


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
    rc, out = sh("pgrep", "-f", "llama-cpp", timeout=10)
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


def warm_once(timeout=300):
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
                try:
                    warm_once(timeout=300)
                    _keepwarm["last_ping"] = now_iso()
                    _keepwarm["last_error"] = None
                except Exception as exc:
                    _keepwarm["last_error"] = f"{type(exc).__name__}: {exc}"
        except Exception:
            pass
        time.sleep(KEEPWARM_PING_INTERVAL)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def build_state():
    loaded_ok, loaded = localai_api_ok()
    pids = backend_pids()
    ac = on_ac_power()
    with _clock:
        counters = dict(_counters)
    return {
        "at": now_iso(),
        "model": MODEL,
        "localai_loaded": localai_loaded(),
        "localai_api": loaded_ok,
        "model_resident": MODEL in loaded,
        "loaded_models": loaded,
        "backend_pids": pids,
        "backend_alive": bool(pids),
        "caffeinate_held": caffeinate_held(),
        "caffeinate_since": _caffeinate["since"],
        "on_ac_power": ac,
        "keep_warm": {
            "active": keepwarm_active(),
            "until": _keepwarm["until"] or None,
            "seconds_left": max(0, int(_keepwarm["until"] - time.time())) if keepwarm_active() else 0,
            "last_ping": _keepwarm["last_ping"],
            "last_error": _keepwarm["last_error"],
            "interval": KEEPWARM_PING_INTERVAL,
        },
        "gpu_bytes": gpu_bytes(),
        "counters": counters,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "qwen-control"
    protocol_version = "HTTP/1.1"

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
        # Loopback is the menu bar app on this same machine; everything arriving
        # over the tailnet must present the shared token.
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1"):
            return True
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(TOKEN) and got == TOKEN

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode()) if n else {}
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/health":
            self._json(200, {"status": "ok", "service": "qwen-control"})
            return
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        if path in ("/", "/state"):
            self._json(200, build_state())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        path = self.path.split("?")[0].rstrip("/")
        body = self._body()

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
        else:
            self._json(404, {"error": "not found"})
            return

        self._json(200 if ok else 500, {"ok": ok, "detail": detail, "state": build_state()})


def main():
    _load_counters()
    threading.Thread(target=keepwarm_loop, daemon=True).start()

    # Two listeners, not one. The auth model trusts 127.0.0.1 so the menu bar app
    # doesn't have to hold a secret, and the menu bar app talks to loopback -- so
    # binding only the Tailscale IP left it unable to reach its own agent. Bind
    # both explicitly rather than 0.0.0.0, which would expose this to the LAN.
    servers = []
    hosts = ["127.0.0.1"]
    if BIND_HOST and BIND_HOST != "127.0.0.1":
        hosts.append(BIND_HOST)

    for host in hosts:
        try:
            srv = ThreadingHTTPServer((host, PORT), Handler)
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
