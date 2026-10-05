#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host resource -> KOReader LAN bridge.

The PC-side half of the Kindle resource monitor. A sampler thread reads
CPU / memory / disk / network / top-processes every few seconds (pure
stdlib on Linux; psutil when installed), and a tiny stdlib HTTP server
lets the KOReader plugin on the Kindle pull:

  GET  /status.json         -> latest snapshot (local machine)
       /status.json?host=H  -> latest report pushed by remote agent H
  GET  /cover.png?w=&h=&theme=&mono=1[&host=H]
       -> the snapshot rendered as an 8-bit grayscale e-ink PNG
  POST /report              -> remote agent pushes its snapshot
       {"host": "vm2", "status": {...}}
  GET  /health              -> {"ok": true}

Run:  python3 res-bridge.py
Env:  RES_PORT        (default 8865)
      RES_TOKEN       (optional simple Bearer auth, like WB_TOKEN upstream)
      RES_SAMPLER_SEC (default 5)

The remote-agent piece is optional: without it the bridge simply monitors
the machine it runs on. `res_report.py` is the agent you point at this
bridge from another host.
"""
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# pythonw / no-console launchers have stdout=None; any print() would then
# raise and kill the process right after bind (same guard as upstream).
if sys.stdout is None or sys.stderr is None:
    _crashlog = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "res_bridge_stdout.log"),
                     "w", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _crashlog
    if sys.stderr is None:
        sys.stderr = _crashlog

try:
    import res_collect
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import res_collect

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("RES_PORT", "8865"))
TOKEN = os.environ.get("RES_TOKEN", "")
LOGPATH = os.path.join(DATA_DIR, "res_bridge.log")
LOG_MAX_BYTES = 512 * 1024    # rotate to res_bridge.log.1 beyond this
REMOTE_STALE_SEC = 300          # a pushed report older than this = "STALE"
REMOTE_MAX_HOSTS = 8
REPORT_MAX_BYTES = 1024 * 1024  # reject absurd POST /report bodies


def _log(msg):
    try:
        if (os.path.exists(LOGPATH)
                and os.path.getsize(LOGPATH) > LOG_MAX_BYTES):
            os.replace(LOGPATH, LOGPATH + ".1")
        with open(LOGPATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---- sampler state -----------------------------------------------------------

_lock = threading.Lock()
_snap = {"snap": None, "error": None}
_remote = {}          # host -> {"status": dict, "ts": float}


def _publisher():
    try:
        sampler = res_collect.make_sampler()
        snap = sampler.sample()          # prime (CPU/net are first-tick zeros)
        with _lock:
            _snap["snap"] = snap
            _snap["error"] = None
        _thread = threading.Thread(
            target=_sampler_loop, args=(sampler,), daemon=True)
        _thread.start()
        print("resource backend:", snap.get("backend"), flush=True)
    except Exception as e:
        with _lock:
            _snap["error"] = str(e)
        print("WARN: resource sampler unavailable: %s" % e, flush=True)


def _sampler_loop(sampler):
    interval = float(os.environ.get("RES_SAMPLER_SEC", "5"))
    while True:
        time.sleep(interval)
        try:
            snap = sampler.sample()
        except Exception as e:
            _log("SAMPLER FAIL %s" % e)
            continue
        with _lock:
            _snap["snap"] = snap


def current_status(host=None):
    """Local snapshot, or the freshest remote report for `host`."""
    with _lock:
        if host:
            r = _remote.get(host)
            if r:
                st = dict(r["status"])
                st["live"] = (time.time() - r["ts"]) <= REMOTE_STALE_SEC
                st["remote"] = host
                return st
            return {"source": "host", "live": False, "host": host,
                    "error": "no report from host '%s'" % host}
        snap = _snap["snap"]
        err = _snap["error"]
    if snap is None:
        return {"source": "host", "live": False, "host": "local",
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "error": err or "sampler not ready yet"}
    return snap


# ---- HTTP --------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj, raw=None, ctype="application/json"):
        body = raw if raw is not None else \
            json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if not TOKEN:
            return True
        ah = self.headers.get("Authorization", "")
        return hmac.compare_digest(ah, "Bearer " + TOKEN)

    def do_GET(self):
        u = urlparse(self.path)
        _log("REQ from %s  %s" % (self.client_address[0], self.path))
        if u.path in ("/status.json", "/status"):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            qs = parse_qs(u.query)
            host = (qs.get("host") or [None])[0]
            self._send(200, current_status(host))
        elif u.path == "/cover.png":
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            try:
                from res_cover import render_res_cover
                qs = parse_qs(u.query)
                w = int(qs.get("w", ["1080"])[0])
                h = int(qs.get("h", ["1440"])[0])
                w = max(200, min(w, 3000))
                h = max(200, min(h, 4000))
                mono = qs.get("mono", ["1"])[0] != "0"
                theme = qs.get("theme", ["dark"])[0]
                if theme not in ("dark", "light"):
                    theme = "dark"
                ehint = qs.get("exit", ["1"])[0] != "0"
                host = (qs.get("host") or [None])[0]
                t0 = time.time()
                png = render_res_cover(current_status(host), w=w, h=h,
                                       mono=mono, theme=theme,
                                       exit_hint=ehint)
                _log("COVER ok %d bytes in %.2fs (w=%d h=%d theme=%s%s)"
                     % (len(png), time.time() - t0, w, h, theme,
                        " host=%s" % host if host else ""))
                self._send(200, None, raw=png, ctype="image/png")
            except Exception as e:
                _log("COVER FAIL %s" % e)
                self._send(500, {"error": str(e)})
        elif u.path == "/health":
            self._send(200, {"ok": True,
                              "backend": (_snap.get("snap") or {}).get("backend"),
                              "remotes": list(_remote.keys())})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/report":
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            try:
                ln = int(self.headers.get("Content-Length", "0") or "0")
                if ln > REPORT_MAX_BYTES:
                    return self._send(413, {"error": "payload too large"})
                raw = self.rfile.read(ln) if ln else b"{}"
                payload = json.loads(raw or b"{}")
            except Exception as e:
                return self._send(400, {"error": str(e)})
            st = payload.get("status")
            host = str(payload.get("host") or (st or {}).get("host")
                      or "unknown")[:64]
            if not isinstance(st, dict):
                return self._send(400, {"error": "status must be an object"})
            with _lock:
                if len(_remote) >= REMOTE_MAX_HOSTS and host not in _remote:
                    # drop the oldest report so a new host can still register
                    oldest = min(_remote, key=lambda k: _remote[k]["ts"])
                    _remote.pop(oldest, None)
                _remote[host] = {"status": st, "ts": time.time()}
            _log("REPORT from %s (%s) saved" % (self.client_address[0], host))
            self._send(200, {"ok": True, "host": host})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    _publisher()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        print("host-resource bridge listening on http://0.0.0.0:%d" % PORT,
              flush=True)
        print("  status : http://<this-host-ip>:%d/status.json" % PORT)
        print("  cover  : http://<this-host-ip>:%d/cover.png?w=1080&h=1440"
              % PORT)
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    except Exception:
        import traceback
        print("!! bridge crashed:", flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        raise SystemExit(1)
