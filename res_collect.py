#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host resource sampler: CPU / memory / disk / network / top processes.

Backend selection (automatic, per machine):
  * psutil  -> used if importable (any platform, best coverage)
  * /proc   -> pure-stdlib Linux fallback (no third-party deps at all)

The sampler is STATEFUL: CPU% / network Kbps are computed as deltas between
ticks, so call sample() periodically (the bridge runs it on a 5s thread).
The very first sample() primes the state and returns zeros for those fields.

Output schema (what the bridge serves and res_cover.py renders):
  {
    "source": "host", "host": "box", "live": true, "backend": "psutil|proc",
    "updatedAt": "2026-10-05 12:00:00",
    "uptime": "3d 4h 12m",
    "cpu":  {"pct": 35.0, "n": 8, "cores": [40, 30, ...],
             "load1": 1.2, "load5": 0.8, "load15": 0.6, "tempC": 52.3},
    "mem":  {"usedGB": 5.1, "totalGB": 7.6, "pct": 67.5,
             "swapUsedGB": 0.2, "swapTotalGB": 2.0, "swapPct": 10.0},
    "disk": [{"mnt": "/", "totalGB": 100.0, "usedGB": 50.0,
              "pct": 52.0, "fs": "ext4"}],
    "net":  {"upKbps": 120.5, "downKbps": 340.2,
             "hist_down": [60 samples], "hist_up": [60 samples]},
    "procs":[{"name": "python3", "pid": 1234, "cpu": 23.5, "memMB": 512.0}],
    "battery": {"pct": 80, "charging": true},
    "alarms": ["MEM 92% >= 90%"]
  }
Empty lists / nulls are legal: the renderer must survive each field being
absent (unmounted USB drive, no thermal zone, plain desktop without battery).
"""
import glob
import os
import platform
import shutil
import socket
import time
from collections import deque

try:
    import psutil
except Exception:
    psutil = None

HIST_LEN = 60          # sparkline history kept in memory (one sample per tick)
DISK_MAX = 4           # disk rows shown (renderer cap)
PROC_MAX = 10          # top-mem processes shown
DISK_FS = ("ext4", "ext3", "xfs", "btrfs", "zfs", "apfs", "ntfs", "ntfs3",
           "vfat", "exfat", "f2fs", "jfs", "reiserfs", "cifs", "fuse.")
NET_SKIP = ("lo", "docker", "veth", "br-", "cni", "tap", "tun", "virbr", "ltx")
# alarm thresholds (mirrored by the cover renderer; keep in sync)
ALARM_MEM_PCT = 90.0
ALARM_DISK_PCT = 90.0
ALARM_CPU_PCT = 95.0
ALARM_SWAP_PCT = 50.0


def _fmt_uptime(sec):
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return "%dd %dh %dm" % (d, h, m)
    if h:
        return "%dh %dm" % (h, m)
    return "%dm" % m


# ---------------------------------------------------------------------------
# /proc (stdlib, Linux) backend
# ---------------------------------------------------------------------------

def _read_lines(path):
    try:
        with open(path, "r") as f:
            return f.read().splitlines()
    except Exception:
        return []


class ProcSampler:
    """Pure-stdlib Linux sampler reading /proc, /sys, os.getloadavg()."""

    def __init__(self):
        self._last_cpu = None       # (all, per_core) jiffies + ts
        self._last_net = None       # {iface: (rx, tx)} + ts
        self._last_hist = time.time()
        self._last_pid = {}         # pid -> (total_jiffies, name, memMB)
        self.hist_down = deque([0.0] * HIST_LEN, maxlen=HIST_LEN)
        self.hist_up = deque([0.0] * HIST_LEN, maxlen=HIST_LEN)
        self._prime()

    # -- prime: take the first absolute snapshot so the 2nd tick has a delta
    def _prime(self):
        self._last_cpu = self._read_cpu()
        self._last_net = self._read_net()
        self._last_hist = time.time()
        self._scan_procs(0.2)            # primes the pid map; output unused

    # ---- cpu ---------------------------------------------------------------
    @staticmethod
    def _read_cpu():
        allf = None
        per = []
        for ln in _read_lines("/proc/stat"):
            if not ln.startswith("cpu"):
                continue
            parts = ln.split()
            if parts[0] == "cpu":
                vals = [int(x) for x in parts[1:]]
                allf = vals
            else:
                per.append([int(x) for x in parts[1:]])
        return allf, per

    @staticmethod
    def _pct(delta):
        tot = sum(delta)
        if tot <= 0:
            return 0.0
        idle = delta[3] + (delta[4] if len(delta) > 4 else 0)
        return max(0.0, min(100.0, (tot - idle) / tot * 100.0))

    # ---- network -----------------------------------------------------------
    @staticmethod
    def _read_net():
        out = {}
        for ln in _read_lines("/proc/net/dev"):
            if ":" not in ln:
                continue
            iface, rest = ln.split(":", 1)
            iface = iface.strip()
            if iface.startswith(NET_SKIP):
                continue
            f = rest.split()
            out[iface] = (int(f[0]), int(f[8]))   # rx_bytes, tx_bytes
        return out

    # ---- processes ---------------------------------------------------------
    def _scan_procs(self, interval):
        """Read jiffies + rss for the tracked pid set, update the map.

        Tracked set = every pid seen last tick + up to 256 new pids per
        tick. CPU% is a per-tick delta over `interval` seconds (convention:
        100% = one full core); `interval` MUST come from the caller's tick
        spacing -- deriving it here (now - self._last_hist) reads a value
        the caller has already refreshed, flooring it to 0.2s and inflating
        every CPU% by ~25x.
        """
        hz = os.sysconf("SC_CLK_TCK")
        new = {}
        keep = set(self._last_pid.keys())
        try:
            pids = [d for d in os.listdir("/proc") if d.isdigit()]
        except Exception:
            pids = []
        new_pids = 0
        for pid_s in pids:
            pid = int(pid_s)
            if pid not in keep and new_pids >= 256:
                continue
            if pid not in keep:
                new_pids += 1
            try:
                with open("/proc/%s/stat" % pid_s, "r") as f:
                    st = f.read()
                rp = st.rfind(")")
                fields = st[rp + 2:].split()
                total = int(fields[11]) + int(fields[12])          # utime+stime
                name = st[st.find("(") + 1:st.find(")")]
                with open("/proc/%s/status" % pid_s, "r") as f:
                    rss = 0.0
                    for sl in f:
                        if sl.startswith("VmRSS:"):
                            rss = int(sl.split()[1]) / 1024.0      # KB -> MB
                            break
                new[pid] = (total, name, rss)
            except Exception:
                continue
        # sanity budget: all cores together can spend at most
        # hz*interval*ncores jiffies in one tick; anything beyond that
        # (pid reuse across ticks, counter weirdness) is clamped to 100%
        budget = hz * interval * (os.cpu_count() or 1)
        out = {}
        for pid, (total, name, rss) in new.items():
            p = self._last_pid.get(pid)
            if p and total >= p[0]:
                dproc = total - p[0]
                if dproc <= budget:
                    cpu = dproc / (hz * interval) * 100.0
                else:
                    cpu = 100.0
                out[pid] = {"name": name, "pid": pid,
                            "cpu": round(cpu, 1),
                            "memMB": round(rss, 1)}
        self._last_pid = new
        return out

    # ---- misc ---------------------------------------------------------------
    @staticmethod
    def _meminfo():
        kv = {}
        for ln in _read_lines("/proc/meminfo"):
            k, _, v = ln.partition(":")
            kv[k] = int(v.split()[0]) * 1024        # kB -> bytes
        return kv

    @staticmethod
    def _disks():
        """Real filesystem mounts only (filter /proc/mounts, dedupe, cap)."""
        seen = {}
        for ln in _read_lines("/proc/mounts"):
            f = ln.split()
            if len(f) < 3:
                continue
            dev, mnt, fs = f[0], f[1], f[2]
            if mnt.startswith(("/proc", "/sys", "/dev", "/run", "/snap")):
                continue
            if not any(fs == p or fs.startswith(p) for p in DISK_FS):
                continue
            key = (dev, fs)
            if key in seen:
                continue
            try:
                du = shutil.disk_usage(mnt)
            except Exception:
                continue
            if du.total <= 0:
                continue
            seen[key] = {"mnt": mnt, "totalGB": round(du.total / 2 ** 30, 1),
                         "usedGB": round(du.used / 2 ** 30, 1),
                         "pct": round(min(100.0, du.used / du.total * 100.0), 1),
                         "fs": fs}
        rows = sorted(seen.values(), key=lambda r: (r["mnt"] != "/", r["mnt"]))
        return rows[:DISK_MAX]

    @staticmethod
    def _battery():
        for base in glob.glob("/sys/class/power_supply/BAT*"):
            cap = _read_lines(base + "/capacity")
            if cap:
                charging = False
                for ln in _read_lines(base + "/status"):
                    charging = "Charging" in ln
                return {"pct": int(cap[0]), "charging": charging}
        return None

    @staticmethod
    def _temp():
        vals = []
        for z in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            v = _read_lines(z)
            if v:
                try:
                    vals.append(int(v[0]) / 1000.0)
                except Exception:
                    pass
        if not vals:
            return None
        return round(max(vals), 1)

    # ---- main ----------------------------------------------------------------
    def sample(self):
        now = time.time()
        interval = max(0.2, now - self._last_hist)
        mem = self._meminfo()
        mem_total = mem.get("MemTotal", 0)
        mem_avail = mem.get("MemAvailable", 0)
        used = max(0, mem_total - mem_avail)
        swap_total = mem.get("SwapTotal", 0)
        swap_free = mem.get("SwapFree", 0)

        cur_cpu, cur_per = self._read_cpu()
        cpu_pct = 0.0
        cores = []
        if self._last_cpu and cur_cpu and self._last_cpu[0] and cur_per:
            last_per = self._last_cpu[1]
            for i, c in enumerate(cur_per):
                lp = last_per[i] if i < len(last_per) else c
                cores.append(round(self._pct([b - a for a, b in
                                               zip(lp, c)]), 1))
            # total = mean of per-core: consistent with the core bars drawn
            # next to it (the aggregate "cpu" line counts guest/iowait time
            # differently and disagrees with the cpu0..N lines)
            cpu_pct = round(sum(cores) / len(cores), 1)
        self._last_cpu = (cur_cpu, cur_per)

        cur_net = self._read_net()
        up_k = down_k = 0.0
        if self._last_net:
            for iface, (rx, tx) in cur_net.items():
                p = self._last_net.get(iface)
                if p:
                    down_k += max(0.0, rx - p[0]) / interval / 1024.0
                    up_k += max(0.0, tx - p[1]) / interval / 1024.0
        self._last_net = cur_net
        self._last_hist = now
        self.hist_down.append(round(down_k, 1))
        self.hist_up.append(round(up_k, 1))

        procs = list(self._scan_procs(interval).values())
        procs.sort(key=lambda p: (-p["memMB"], -p["cpu"]))
        procs = [p for p in procs[:PROC_MAX] if p["name"] != "swapper"]

        try:
            l1, l5, l15 = os.getloadavg()
        except AttributeError:
            l1 = l5 = l15 = 0.0
        try:
            with open("/proc/uptime", "r") as f:
                uptime_s = float(f.read().split()[0])
        except Exception:
            uptime_s = 0.0

        snap = {
            "source": "host",
            "host": socket.gethostname(),
            "live": True,
            "backend": "proc",
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "uptime": _fmt_uptime(uptime_s),
            "cpu": {
                "pct": round(cpu_pct, 1),
                "n": len(cur_per) if cur_per else os.cpu_count() or 1,
                "cores": cores,
                "load1": l1, "load5": l5, "load15": l15,
                "tempC": self._temp(),
            },
            "mem": {
                "usedGB": round(used / 2 ** 30, 1),
                "totalGB": round(mem_total / 2 ** 30, 1),
                "pct": round(used / mem_total * 100.0, 1) if mem_total else 0.0,
                "swapUsedGB": round((swap_total - swap_free) / 2 ** 30, 1),
                "swapTotalGB": round(swap_total / 2 ** 30, 1),
                "swapPct": (round((swap_total - swap_free) / swap_total * 100.0, 1)
                            if swap_total else 0.0),
            },
            "disk": self._disks(),
            "net": {"upKbps": round(up_k, 1), "downKbps": round(down_k, 1),
                    "hist_down": list(self.hist_down),
                    "hist_up": list(self.hist_up)},
            "procs": procs,
            "battery": self._battery(),
        }
        snap["alarms"] = _alarms(snap)
        return snap


def _alarms(snap):
    out = []
    m = snap.get("mem") or {}
    if m.get("pct", 0) >= ALARM_MEM_PCT:
        out.append("MEM %d%% >= %d%%" % (int(m["pct"]), int(ALARM_MEM_PCT)))
    for d in snap.get("disk") or []:
        if d.get("pct", 0) >= ALARM_DISK_PCT:
            out.append("DISK %s %d%% >= %d%%"
                      % (d.get("mnt", "?"), int(d["pct"]),
                         int(ALARM_DISK_PCT)))
    c = snap.get("cpu") or {}
    if c.get("pct", 0) >= ALARM_CPU_PCT:
        out.append("CPU %d%% SATURATED" % int(c["pct"]))
    if m.get("swapPct", 0) >= ALARM_SWAP_PCT:
        out.append("SWAP %d%%" % int(m["swapPct"]))
    n = c.get("n") or 1
    if c.get("load1", 0) >= n * 2:
        out.append("LOAD %.1f HIGH (cores=%d)" % (c["load1"], n))
    return out


# ---------------------------------------------------------------------------
# psutil backend (any platform; used when psutil is importable)
# ---------------------------------------------------------------------------

class PsutilSampler:
    def __init__(self):
        self.hist_down = deque([0.0] * HIST_LEN, maxlen=HIST_LEN)
        self.hist_up = deque([0.0] * HIST_LEN, maxlen=HIST_LEN)
        # prime: first cpu_percent()/net_io/process_iter calls return None
        psutil.cpu_percent(interval=None, percpu=True)
        psutil.cpu_percent(interval=None)
        psutil.net_io_counters()
        for p in psutil.process_iter(["cpu_percent", "memory_info",
                                      "name", "pid"]):
            try:
                p.cpu_percent(interval=None)
            except Exception:
                pass

    def sample(self):
        cpu_all = psutil.cpu_percent(interval=None)
        cores = [round(v, 1) for v in
                 psutil.cpu_percent(interval=None, percpu=True)]
        vm = psutil.virtual_memory()
        try:
            sw = psutil.swap_memory()
        except Exception:
            sw = None
        net = psutil.net_io_counters()
        # delta vs previous net tick: approximate interval via time between
        # sampler calls; keep the previous counter on the instance
        up_k = down_k = 0.0
        if getattr(self, "_last_net", None):
            lts, lrx, ltx = self._last_net
            interval = max(0.2, time.time() - lts)
            down_k = max(0.0, net.bytes_recv - lrx) / interval / 1024.0
            up_k = max(0.0, net.bytes_sent - ltx) / interval / 1024.0
        self._last_net = (time.time(), net.bytes_recv, net.bytes_sent)
        self.hist_down.append(round(down_k, 1))
        self.hist_up.append(round(up_k, 1))

        top = []
        for p in psutil.process_iter(["cpu_percent", "memory_info",
                                      "name", "pid"]):
            try:
                info = p.info
                if not info["name"]:
                    continue
                top.append({"name": info["name"][:24],
                            "pid": info["pid"],
                            "cpu": round(info.get("cpu_percent") or 0.0, 1),
                            "memMB": round((info.get("memory_info") or
                                            None) and
                                           info["memory_info"].rss / 2 ** 20
                                           or 0.0, 1)})
            except Exception:
                continue
        top.sort(key=lambda p: (-p["memMB"], -p["cpu"]))
        top = [p for p in top[:PROC_MAX] if p["name"] not in ("swapper",)]

        try:
            l1, l5, l15 = os.getloadavg()
        except AttributeError:
            l1 = l5 = l15 = 0.0

        snap = {
            "source": "host",
            "host": socket.gethostname(),
            "live": True,
            "backend": "psutil",
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "uptime": _fmt_uptime(psutil.boot_time() and
                                  (time.time() - psutil.boot_time())),
            "cpu": {
                "pct": round(cpu_all, 1),
                "n": len(cores) or os.cpu_count() or 1,
                "cores": cores,
                "load1": l1, "load5": l5, "load15": l15,
                "tempC": self._psutil_temp(),
            },
            "mem": {
                "usedGB": round(vm.total * vm.percent / 100.0 / 2 ** 30, 1),
                "totalGB": round(vm.total / 2 ** 30, 1),
                "pct": round(vm.percent, 1),
            "swapUsedGB": round(sw.used / 2 ** 30, 1) if sw else 0.0,
            "swapTotalGB": round(sw.total / 2 ** 30, 1) if sw else 0.0,
                "swapPct": round(sw.percent, 1) if sw else 0.0,
            },
            "disk": self._psutil_disks(),
            "net": {"upKbps": round(up_k, 1), "downKbps": round(down_k, 1),
                    "hist_down": list(self.hist_down),
                    "hist_up": list(self.hist_up)},
            "procs": top,
            "battery": self._psutil_battery(),
        }
        snap["alarms"] = _alarms(snap)
        return snap

    @staticmethod
    def _psutil_temp():
        try:
            ts = psutil.sensors_temperatures()
            for key in ("coretemp", "x86_pkg_temp", "cpu_thermal", "k10temp"):
                if key in ts:
                    return round(max(t.temperature for t in ts[key]), 1)
            vals = [t.temperature for l in ts.values() for t in l]
            return round(max(vals), 1) if vals else None
        except Exception:
            return None

    @staticmethod
    def _psutil_disks():
        rows = []
        for p in psutil.disk_partitions(all=False):
            if not any(p.fstype == f or p.fstype.startswith(f + ".")
                       for f in DISK_FS):
                continue
            try:
                du = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            if du.total <= 0:
                continue
            rows.append({"mnt": p.mountpoint,
                         "totalGB": round(du.total / 2 ** 30, 1),
                         "usedGB": round(du.used / 2 ** 30, 1),
                         "pct": round(du.percent, 1),
                         "fs": p.fstype})
        rows.sort(key=lambda r: (r["mnt"] != "/", r["mnt"]))
        return rows[:DISK_MAX]

    @staticmethod
    def _psutil_battery():
        try:
            b = psutil.sensors_battery()
            if b:
                return {"pct": int(b.percent), "charging": bool(b.power_plugged)}
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------

def make_sampler():
    """Pick the best backend available on this machine."""
    if psutil is not None:
        return PsutilSampler()
    if platform.system() == "Linux":
        return ProcSampler()
    raise RuntimeError(
        "no resource backend: install psutil (pip install psutil) "
        "on this platform (%s)" % platform.system())


if __name__ == "__main__":
    import json
    s = make_sampler()
    print(json.dumps(s.sample(), indent=2))
    time.sleep(2)
    print(json.dumps(s.sample(), indent=2))
