#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host-resource HUD cover renderer for the Kindle e-ink monitor.

Renders the host status JSON (see res_collect.py) into a high-contrast,
grayscale "terminal/HUD" cover: big plain-text values, one thin outer frame,
inverted full-width alarm bar, 8-bit "L" PNG out. Pure text by design --
no bars, markers or glyphs, so it stays legible even on a small screen.
"""
import glob
import io
import os
import sys
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ---- font: cross-platform, Latin monospace preferred (HUD look) -----------
FONT_CANDIDATES = [
    # Windows
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/DejaVuSansMono.ttf",
    "C:/Windows/Fonts/lucon.ttf",
    # Linux (distro layouts differ; the glob pass below catches the rest)
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/google-noto/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/droid/DroidSansMono.ttf",
    # macOS
    "/System/Library/Fonts/Menlo.ttc",
]


def _find_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    for pat in ("/usr/share/fonts/**/*Mono*.ttf",
                "/usr/share/fonts/**/DejaVu*.ttf",
                "/usr/share/fonts/**/*.ttf",
                "/usr/share/fonts/**/*.otf",
                "/usr/lib/fonts/**/*.ttf"):
        hits = sorted(glob.glob(pat, recursive=True))
        if hits:
            return hits[0]
    return None


_FONT_PATH = _find_font()


def _font(size):
    size = max(8, int(size))
    if _FONT_PATH:
        try:
            return ImageFont.truetype(_FONT_PATH, size)
        except Exception:
            pass
    try:  # Pillow >= 10.1
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


# ---- palettes (same semantics as the original cover_gen.py) ---------------

def _mono_palette():
    # dark theme: grays pushed toward white -- secondary text at 120/168
    # turned into faint halftone mush on a real e-ink panel
    return {
        "BG": (0, 0, 0), "WHITE": (255, 255, 255), "LGRAY": (205, 205, 205),
        "MGRAY": (165, 165, 165), "DGRAY": (66, 66, 66), "DARK": (0, 0, 0),
        "INK": (0, 0, 0),
    }


def _mono_palette_inverted():
    # light theme: text near-black on pure white; MGRAY 128 -> 85 because
    # mid-gray reads as ~2.8:1 contrast, illegible on e-ink
    return {
        "BG": (255, 255, 255), "WHITE": (0, 0, 0), "LGRAY": (45, 45, 45),
        "MGRAY": (85, 85, 85), "DGRAY": (186, 186, 186),
        "DARK": (235, 235, 235), "INK": (255, 255, 255),
    }


def _color_palette():
    return {
        "BG": (8, 9, 18), "WHITE": (235, 246, 255), "LGRAY": (135, 146, 158),
        "MGRAY": (135, 146, 158), "DGRAY": (70, 82, 94), "DARK": (12, 14, 22),
        "INK": (10, 10, 10),
    }


def _palette(mode="dark", color=False):
    if color:
        return _color_palette()
    return _mono_palette_inverted() if mode == "light" else _mono_palette()


# ---- small drawing helpers -------------------------------------------------

def _text(d, xy, s, size, fill, anchor="lt"):
    d.text(xy, s, font=_font(size), fill=fill, anchor=anchor)


def _fit(d, s, font, maxw):
    """Truncate a string to a measured pixel width (font-agnostic)."""
    while s and d.textlength(s, font=font) > maxw:
        s = s[:-1]
    return s or "?"


def _section_label(d, x, y, label, P, s):
    _text(d, (x, y), label, int(56 * s), P["WHITE"], anchor="lt")
    d.line([(x, y + int(66 * s)), (x + int(600 * s), y + int(66 * s))],
           fill=P["DGRAY"] + (255,), width=1)
    return y + int(78 * s)


# ---- section renderers (design units @1440h, scaled by s) ------------------

def _sec_cpu(d, x0, xr, y, s, st, P):
    cpu = st.get("cpu") or {}
    if not cpu:
        _text(d, (x0, y), "cpu: n/a", int(40 * s), P["MGRAY"])
        return y + int(60 * s)
    y = _section_label(d, x0, y, "CPU", P, s)
    big = "%d%%" % int(round(cpu.get("pct", 0)))
    _text(d, (x0, y - int(4 * s)), big, int(210 * s), P["WHITE"],
          anchor="lt")
    return y + int(224 * s)


def _sec_mem(d, x0, xr, y, s, st, P):
    mem = st.get("mem") or {}
    if not mem:
        return y
    y = _section_label(d, x0, y, "MEM", P, s)
    pct = float(mem.get("pct", 0))
    _text(d, (x0, y),
          "%.1fG / %.1fG   %d%%" % (mem.get("usedGB", 0),
                                    mem.get("totalGB", 0), int(pct)),
          int(60 * s), P["WHITE"], anchor="lt")
    return y + int(100 * s)


def _sec_procs(d, x0, xr, y, s, st, P):
    procs = st.get("procs") or []
    y = _section_label(d, x0, y, "TOP MEM", P, s)
    if not procs:
        _text(d, (x0, y), "- none -", int(36 * s), P["MGRAY"])
        return y + int(56 * s)
    tf = _font(int(46 * s))
    for i, p in enumerate(procs[:10], 1):
        # right columns sized by content so MEM/CPU can never overlap the
        # name column (measured, font-agnostic)
        memmb = p.get("memMB", 0)
        mems = ("%.1fG" % (memmb / 1024.0)) if memmb >= 1024 else \
               ("%dM" % int(memmb))
        memw = d.textlength(mems, font=tf)
        cpua = xr - int(36 * s) - memw
        cpus = "%s%%" % p.get("cpu", 0)
        cpuw = d.textlength(cpus, font=tf)
        name = _fit(d, str(p.get("name", "?")), tf,
                    max(int(60 * s), cpua - cpuw - (x0 + int(64 * s))))
        _text(d, (x0, y), "%d" % i, int(46 * s), P["MGRAY"], anchor="lt")
        _text(d, (x0 + int(56 * s), y), name, int(46 * s), P["LGRAY"],
              anchor="lt")
        _text(d, (cpua, y), cpus, int(46 * s), P["WHITE"], anchor="rt")
        _text(d, (xr, y), mems, int(46 * s), P["WHITE"], anchor="rt")
        y += int(60 * s)
    return y


# ---- main renderer ----------------------------------------------------------

def render_res_cover(status, w=1080, h=1440, mono=True, theme="dark",
                     exit_hint=True):
    """Render the host status dict into a grayscale e-ink safe PNG.
    Returns PNG bytes (8-bit "L" when mono, RGB colour preview otherwise)."""
    P = _palette(theme, color=not mono)
    s = h / 1440.0
    img = Image.new("RGBA", (w, h), P["BG"] + (255,))
    d = ImageDraw.Draw(img)

    m = int(30 * s)
    d.rectangle([m, m, w - m, h - m], outline=P["WHITE"] + (235,),
                width=max(2, int(6 * s)))

    x0 = m + int(46 * s)
    xr = w - m - int(46 * s)
    bottom_limit = h - m - int(80 * s)
    y = m + int(20 * s)

    # ---- alarm strip (inverted full width = loudest on e-ink) ----------------
    alarms = list(status.get("alarms") or [])
    if not status.get("live", True):
        alarms.insert(0, "STALE")
    err = status.get("error")
    if err:
        alarms.append("ERR " + str(err)[:32])
    if alarms:
        msg = "ALARM  " + "  \u00b7  ".join(alarms[:3])
        if len(alarms) > 3:
            msg += "  \u00b7 +%d" % (len(alarms) - 3)
        tf = _font(int(36 * s))
        msg = _fit(d, msg, tf, int(560 * s))
        ay = y + 2
        d.rectangle([m + int(12 * s), ay, w - m - int(12 * s),
                     ay + int(56 * s)], fill=P["WHITE"] + (255,))
        _text(d, ((x0 + xr) // 2, ay + int(28 * s)), msg, int(36 * s),
               P["INK"], anchor="mm")
        y = ay + int(56 * s) + int(18 * s)

    # ---- sections; on a short screen the TOP MEM block is dropped first,
    # ---- never the gauges ----------------------------------------------------
    y = _sec_cpu(d, x0, xr, y, s, status, P)
    y = _sec_mem(d, x0, xr, y, s, status, P)
    if y + int(690 * s) < bottom_limit:
        y = _sec_procs(d, x0, xr, y, s, status, P)

    # ---- footer ----------------------------------------------------------------
    fy = h - m - int(56 * s)
    d.line([(m + int(12 * s), fy - int(18 * s)),
            (w - m - int(12 * s), fy - int(18 * s))],
           fill=P["WHITE"] + (150,), width=2)
    up = status.get("uptime") or "?"
    _text(d, (x0, fy),
          "UP %s   SRC:%s" % (up, str(status.get("source", "?")).upper()),
          int(34 * s), P["MGRAY"], anchor="lt")
    if exit_hint:
        _text(d, (xr, fy), "EXIT: NEXT x3", int(32 * s), P["MGRAY"],
              anchor="rt")

    if mono:
        out = img.convert("L")
        try:
            out = ImageOps.autocontrast(out, cutoff=0)
        except Exception:
            pass
    else:
        out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


# ---- sample data (offline preview + smoke test) -----------------------------

def sample_status():
    return {
        "source": "host",
        "host": "johnson-pc",
        "live": True,
        "backend": "proc",
        "updatedAt": "2026-10-05 18:35:05",
        "uptime": "51d 22h 56m",
        "cpu": {"pct": 34.0, "n": 8,
                "cores": [92, 15, 48, 8, 61, 22, 5, 37],
                "load1": 2.31, "load5": 1.87, "load15": 1.42, "tempC": 52.4},
        "mem": {"usedGB": 4.9, "totalGB": 7.6, "pct": 64.5,
                "swapUsedGB": 0.6, "swapTotalGB": 3.0, "swapPct": 20.0},
        "disk": [
            {"mnt": "/", "totalGB": 294.2, "usedGB": 47.1, "pct": 16.0,
             "fs": "ext4"},
            {"mnt": "/home", "totalGB": 476.9, "usedGB": 402.3, "pct": 84.0,
             "fs": "ext4"},
            {"mnt": "/mnt/usb", "totalGB": 18.4, "usedGB": 12.1, "pct": 66.0,
             "fs": "ext4"},
        ],
        "net": {"upKbps": 120.5, "downKbps": 340.2,
                "hist_down": [0, 5, 12, 8, 30, 45, 22, 90, 180, 95,
                               30, 60, 210, 480, 340, 120, 60, 340.2] +
                [0.0] * 42,
                "hist_up": [0] * 60},
        "procs": [
            {"name": "chrome", "pid": 2211, "cpu": 23.5, "memMB": 1450.2},
            {"name": "code", "pid": 1180, "cpu": 12.1, "memMB": 980.4},
            {"name": "docker", "pid": 445, "cpu": 6.8, "memMB": 512.0},
            {"name": "gnome-shell", "pid": 3001, "cpu": 3.1, "memMB": 156.3},
            {"name": "python3", "pid": 77, "cpu": 2.2, "memMB": 120.6},
        ],
        "alarms": [],
    }


def sample_status_alarm():
    st = sample_status()
    st["mem"]["pct"] = 93.0
    st["mem"]["usedGB"] = 7.1
    st["mem"]["swapUsedGB"] = 1.8
    st["mem"]["swapPct"] = 61.0
    st["alarms"] = ["MEM 93% >= 90%", "SWAP 61%"]
    st["disk"][1]["pct"] = 91.0
    return st


if __name__ == "__main__":
    outdir = sys.argv[1] if len(sys.argv) > 1 else "assets"
    os.makedirs(outdir, exist_ok=True)
    for name, st, theme in (("example_host_dark.png", sample_status(), "dark"),
                            ("example_host_light.png", sample_status(),
                             "light"),
                            ("example_host_alarm.png",
                             sample_status_alarm(), "dark")):
        png = render_res_cover(st, theme=theme)
        with open(os.path.join(outdir, name), "wb") as f:
            f.write(png)
        print("wrote", name, len(png), "bytes")
