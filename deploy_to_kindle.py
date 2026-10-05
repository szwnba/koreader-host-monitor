"""Copy the plugin onto the Kindle, working around KOReader's file lock.

Why this script exists
----------------------
KOReader keeps a plugin's main.lua / config.txt open while it is running.
On that state:

  * open(dst, "wb")            -> Permission denied [Errno 13]
  * cp -f                      -> "cannot remove ...: Permission denied"
  * os.rename(dst, dst+".old") -> Permission denied

but the *directory* is still writable (you can create new files in it), and
deleting the target works once KOReader lets go.

The reliable recipe is therefore DELETE-FIRST, THEN WRITE FRESH:

    rm -f dst ; cp src dst

Plain overwrite retries forever and never succeeds; rm-then-write succeeds as
soon as the lock is released. (Also note: a plain `cp -f` that reports success
may have only *partially* failed -- always `cmp` afterwards.)

Usage:  python3 deploy_to_kindle.py            # 30 min retry window
        python3 deploy_to_kindle.py --once     # single attempt, no retry

The Kindle mount point is a path:  KINDLE_MOUNT=/media/you/Kindle
                                    python3 deploy_to_kindle.py
(Windows: KINDLE_MOUNT=F:/  -- the letter must be absolute as `F:/`.)
The plugin is written to  <mount>/koreader/plugins/host_monitor.koplugin/
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "host_monitor.koplugin")


def _default_mount():
    m = os.environ.get("KINDLE_MOUNT")
    if m:
        return m
    if os.name == "nt":
        return "F:/"
    # Linux: probe the usual desktop-automount spots for a Kindle
    import glob
    for pat in ("/media/*/Kindle", "/run/media/*/Kindle", "/media/*/kindle"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return ""


MOUNT = _default_mount()
DST = os.path.join(MOUNT, "koreader", "plugins", "host_monitor.koplugin") \
    if MOUNT else ""
FILES = ["main.lua", "_meta.lua", "config.txt"]


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def deploy_one(name, data):
    """Delete-first write. Returns True when dst now matches `data`."""
    dst = os.path.join(DST, name)
    try:
        os.makedirs(DST, exist_ok=True)
        if os.path.exists(dst):
            os.remove(dst)                      # <- the trick
        with open(dst, "wb") as f:
            f.write(data)
    except Exception as e:
        print("retry", name, e, flush=True)
        return False
    return _read(dst) == data


def main():
    once = "--once" in sys.argv
    deadline = time.time() + 1800               # 30 min
    if not os.path.isdir(SRC):
        print("ERROR: source plugin dir not found:", SRC, flush=True)
        return 1
    if not MOUNT:
        print("ERROR: Kindle mount point not found automatically.\n"
              "Plug the Kindle in (USB), or set it explicitly, e.g.:\n"
              "  KINDLE_MOUNT=/media/youruser/Kindle python3 deploy_to_kindle.py",
              flush=True)
        return 1
    print("mount:", MOUNT, "-> dst:", DST, flush=True)
    while True:
        # Re-read the source every round: the local copy keeps changing while
        # we wait for the Kindle to release its own.
        src_data = {}
        for name in FILES:
            src_data[name] = _read(os.path.join(SRC, name))
        if any(v is None for v in src_data.values()):
            print("ERROR: missing source file:", flush=True)
            return 1
        pending = [n for n in FILES
                   if src_data[n] != _read(os.path.join(DST, n))]
        if not pending:
            print("DEPLOYED AND VERIFIED: all files identical", flush=True)
            return 0
        for name in pending:
            if deploy_one(name, src_data[name]):
                print("wrote", name, flush=True)
        if once:
            print("single attempt finished; still pending:", pending, flush=True)
            return 1
        if time.time() > deadline:
            print("TIMEOUT: still locked, user action needed", flush=True)
            return 1
        time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
