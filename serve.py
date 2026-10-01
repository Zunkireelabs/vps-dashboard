#!/usr/bin/env python3
"""
Live VPS dashboard server — two speeds.

    python3 dashboard/serve.py                          # http://127.0.0.1:8765
    python3 dashboard/serve.py --interval 180 --fast 60
    VPS_LOCAL=1 python3 dashboard/serve.py ...          # when running ON the VPS (no SSH)

  • Full scan  every --interval s (default 180): reads the server (containers, RAM, disk, ports),
    tests every URL incl. DNS + certificates, saves the numbers to history.
  • Fast probe every --fast s (default 60): one HTTPS request per site. A site is only marked Down
    after 2 failures in a row. Feeds the "uptime %" and updates the page between full scans.

The page swaps new data in by itself — no reload. Read-only: nothing on the server is changed.

Safe by default: listens on 127.0.0.1 only. A network address REQUIRES DASH_PASSWORD
(the page lists your whole infrastructure).
"""
import argparse
import base64
import hmac
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build  # noqa: E402

SCAN_LOCK = threading.Lock()      # one full scan at a time
ASM = threading.RLock()           # guards the snapshot + page while they are rebuilt
WAKE = threading.Event()          # set to start a full scan now
S = dict(snap=None, html=None, version=0, probe_at=0.0, error=None, probe_error=None, refreshing=False,
         seconds=None, last_try=0.0, counts=None, strikes={})
MIN_GAP = 20                      # "Refresh now" can't hammer the server
CFG = dict(interval=180, fast=60, user="team", password="")

WAITING = ("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=4><title>VPS Dashboard</title>"
           "<body style='font:16px -apple-system,sans-serif;padding:60px;color:#444'>"
           "<h2>First check is running…</h2><p>This takes about 20 seconds. The page will load by itself.</p>{err}</body>")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def publish(record):
    """Rebuild the page from the current snapshot and make it the live version."""
    snap = S["snap"]
    ctx = dict(probe_at=S["probe_at"] or snap["at"], version=int(time.time() * 1000),
               stale_probe=max(180, CFG["fast"] * 4), stale_inv=max(600, CFG["interval"] * 3))
    res = build.assemble(snap, record, ctx)
    S.update(html=res["html"], version=ctx["version"], counts=res["counts"])
    tmp = build.OUT.with_suffix(".tmp")
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(res["html"])
    os.replace(tmp, build.OUT)


def scan_once():
    if not SCAN_LOCK.acquire(blocking=False):
        return
    S["refreshing"], S["last_try"] = True, time.time()
    try:
        t0 = time.time()
        snap = build.scan()                              # slow part, outside the lock
        with ASM:
            S["snap"], S["probe_at"] = snap, snap["at"]
            S["strikes"] = {h: S["strikes"].get(h, 0) for h in snap["checks"]}
            publish(record=True)
            S.update(error=None, seconds=round(time.time() - t0, 1))
        log(f"full scan ok in {S['seconds']}s — {S['counts']}")
    except Exception as e:                               # keep serving the last good page
        S["error"] = str(e)[:200]
        log("full scan FAILED:", S["error"])
    finally:
        S["refreshing"] = False
        SCAN_LOCK.release()


def fast_tick():
    snap = S["snap"]
    if not snap:
        return
    work = [(h, dict(c), S["strikes"].get(h, 0)) for h, c in snap["checks"].items()]
    now = time.time()
    with ThreadPoolExecutor(12) as ex:
        results = list(ex.map(lambda w: (w[0], build.fast_probe(*w)), work))
    h = build.hist()
    with ASM:
        if S["snap"] is not snap:                        # a full scan finished meanwhile — its data is newer
            return
        changed = []
        for host, (ok, upd, strikes) in results:
            S["strikes"][host] = strikes
            h.probe(host, now, ok)
            if upd:
                if snap["checks"][host].get("level") != upd["level"]:
                    changed.append(host)
                snap["checks"][host].update(upd)
        S["probe_at"], S["probe_error"] = now, None
        publish(record=False)
    if changed:
        log("site status changed:", ", ".join(sorted(changed)))


def full_loop():
    while True:
        scan_once()
        WAKE.wait(CFG["interval"])
        WAKE.clear()


def fast_loop():
    while True:
        t0 = time.time()
        try:
            fast_tick()
        except Exception as e:
            S["probe_error"] = str(e)[:160]
            log("quick check FAILED:", S["probe_error"])
        time.sleep(max(1, CFG["fast"] - (time.time() - t0)))


def fresh():
    snap = S["snap"]
    if not snap:
        return False, None, None
    pa, ia = time.time() - S["probe_at"], time.time() - snap["at"]
    return pa < max(180, CFG["fast"] * 4) and ia < max(600, CFG["interval"] * 3), round(pa), round(ia)


class H(BaseHTTPRequestHandler):
    server_version = "dash"

    def log_message(self, *a):  # no access log (keeps the terminal readable, nothing sensitive printed)
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        if not CFG["password"]:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                return hmac.compare_digest(u, CFG["user"]) & hmac.compare_digest(p, CFG["password"])
            except Exception:
                return False
        return False

    def _gate(self):
        if self._authed():
            return True
        self._send(401, "Login required", "text/plain", {"WWW-Authenticate": 'Basic realm="VPS dashboard"'})
        return False

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":      # for a Docker healthcheck / autoheal later: 200 only while the data is fresh
            ok, pa, ia = fresh()
            return self._send(200 if ok else 503, json.dumps(dict(ok=ok, sites_age=pa, server_age=ia)), "application/json")
        if not self._gate():
            return
        if path == "/status.json":
            snap = S["snap"]
            return self._send(200, json.dumps(dict(
                version=S["version"], probe_at=S["probe_at"], inv_at=snap["at"] if snap else 0, error=S["error"],
                probe_error=S["probe_error"], refreshing=S["refreshing"], interval=CFG["interval"], fast=CFG["fast"],
                seconds=S["seconds"])), "application/json")
        if path in ("/", "/index.html"):
            if S["html"]:
                return self._send(200, S["html"])
            err = f"<p style='color:#b00'>Last attempt failed: {S['error']}</p>" if S["error"] else ""
            return self._send(200, WAITING.replace("{err}", err))
        self._send(404, "Not found", "text/plain")

    def do_POST(self):
        if not self._gate():
            return
        if self.path.split("?")[0] != "/refresh" or self.headers.get("X-Requested-With") != "dashboard":
            return self._send(403, "Forbidden", "text/plain")   # custom header = same-origin only (no CORS)
        if S["refreshing"] or time.time() - S["last_try"] < MIN_GAP:
            return self._send(202, json.dumps(dict(started=False)), "application/json")
        WAKE.set()
        self._send(202, json.dumps(dict(started=True)), "application/json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=int, default=180, help="seconds between full scans (min 60)")
    ap.add_argument("--fast", type=int, default=60, help="seconds between quick site checks (min 20)")
    a = ap.parse_args()
    CFG["interval"], CFG["fast"] = max(60, a.interval), max(20, a.fast)
    CFG["user"] = os.environ.get("DASH_USER", "team")
    CFG["password"] = os.environ.get("DASH_PASSWORD", "")
    if a.host not in ("127.0.0.1", "localhost", "::1") and not CFG["password"]:
        sys.exit("Refusing to listen on a network address without a password. Set DASH_PASSWORD (and optionally DASH_USER).")
    build.hist()
    threading.Thread(target=full_loop, daemon=True).start()
    threading.Thread(target=fast_loop, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    log(f"Dashboard on http://{a.host}:{a.port} — full scan every {CFG['interval']}s, quick site check every {CFG['fast']}s"
        f"{', login required' if CFG['password'] else ', local only'} (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
