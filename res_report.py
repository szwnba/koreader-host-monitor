#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Remote host agent: collect THIS machine's resources and push them to a
running res-bridge.py, so the Kindle can monitor a machine that is NOT the
bridge host.

Usage:
  python3 res_report.py http://192.168.1.20:8865
  python3 res_report.py http://192.168.1.20:8865 --host vm2 --interval 30
  python3 res_report.py http://192.168.1.20:8865 --once     # single push
Auth: export RES_TOKEN=xxx  (only needed when the bridge set RES_TOKEN)

Daemonize options (pick one):
  * nohup python3 res_report.py URL --host vm2 &
  * cron every minute:    * * * * * python3 /path/res_report.py URL --host vm2 --once
  * systemd unit / pm2 / supervisord -- any wrapper that keeps it alive

On the Kindle side, point the plugin's config.txt at this host with a
`host=vm2` line and the dashboard will show the remote machine instead of
the bridge host.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import res_collect


def push(url, status, token=""):
    data = json.dumps({"host": status.get("host", "unknown"),
                       "status": status}).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/report",
                                 data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read() or b"{}")
    return body.get("ok") is True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bridge_url", help="e.g. http://192.168.1.20:8865")
    ap.add_argument("--host", default=None,
                    help="override the host name shown on the cover")
    ap.add_argument("--interval", type=int, default=30,
                    help="push interval seconds (default 30)")
    ap.add_argument("--once", action="store_true",
                    help="push one snapshot and exit (for cron)")
    args = ap.parse_args()
    token = os.environ.get("RES_TOKEN", "")

    sampler = res_collect.make_sampler()
    sampler.sample()                       # prime the delta state
    # without this the first sample's CPU%/net deltas divide a ~zero-second
    # window (the very bug that made `--once` cron pushes read all zeros)
    time.sleep(2)

    fails = 0
    while True:
        snap = sampler.sample()
        if args.host:
            snap["host"] = args.host
        try:
            ok = push(args.bridge_url, snap, token)
            if not ok:
                raise RuntimeError("bridge replied not-ok")
            if fails:
                print("reconnected after %d failed pushes" % fails,
                      flush=True)
            fails = 0
            print(time.strftime("%H:%M:%S"), "pushed", snap.get("host"),
                  "cpu=%s%% mem=%s%%" % (
                      snap.get("cpu", {}).get("pct"),
                      snap.get("mem", {}).get("pct")), flush=True)
        except Exception as e:
            fails += 1
            print(time.strftime("%H:%M:%S"), "push failed (%s), attempt %d: %s"
                  % (args.bridge_url, fails, e), flush=True)
        if args.once:
            break
        # back off to 60s after 3 consecutive failures (bridge offline)
        time.sleep(args.interval if fails < 3 else 60)


if __name__ == "__main__":
    main()
