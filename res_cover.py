#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host-resource HUD cover renderer for the Kindle e-ink monitor.

Renders the host status JSON (see res_collect.py) into a high-contrast,
grayscale "terminal/HUD" cover in the same visual language as the original
WorkBuddy cover_gen.py: background grid, double frame with corner brackets,
inverted full-width alarm bar, segmented bars, 8-bit "L" PNG out.

CRITICAL: the target is a 1-bit grayscale e-ink panel, so all status is
carried by VALUE + SHAPE, never hue. A colour preview mode (?mono=0) exists
for eyeballing on a PC only.

Layout is designed at 1080x1440 and SCALED: all sizes multiply by h/1440,
so the same layout survives any screen the device reports
(KOReader passes its real pixel size in ?w=&h=).

All cover text is ASCII: a KOReader build without a CJK font still renders
it correctly on the PC side (the font used here is a Latin monospace).
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
    return {
        "BG": (16, 16, 16), "WHITE": (238, 238, 238), "LGRAY": (168, 168, 168),
        "MGRAY": (120, 120, 120), "DGRAY": (66, 66, 66), "DARK": (14, 14, 14),
        "INK": (10, 10, 10),
    }


def _mono_palette_inverted():
    return {
        "BG": (247, 247, 247), "WHITE": (12, 12, 12), "LGRAY": (78, 78, 78),
        "MGRAY": (128, 128, 128), "DGRAY": (186, 186, 186),
        "DARK": (238, 238, 238), "INK": (250, 250, 250),
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


def _corner(d, cx, cy, sx, sy, bl, color):
    d.line([(cx, cy), (cx + sx * bl, cy)], fill=color + (255,), width=bl // 8)
    d.line([(cx, cy), (cx, cy + sy * bl)], fill=color + (255,), width=bl // 8)


def _segbar(d, x, y, w, h, pct, P, n=20):
    """Horizontal segmented bar (n cells). Filled cells = ink, empty = DGRAY.
    The loudest, cheapest gauge shape on e-ink -- no anti-aliasing needed."""
    pct = max(0.0, min(100.0, pct))
    cell = w / n
    gap = max(1.0, cell * 0.18)
    filled = int(round(pct / 100.0 * n))
    for i in range(n):
        x0 = x + i * cell + gap / 2
        x1 = x + (i + 1) * cell - gap / 2
        col = P["WHITE"] if i < filled else P["DGRAY"]
        d.rectangle([x0, y, x1, y + h], fill=col + (255,))


def _spark(d, x, y, w, h, vals, P, n=60):
    """Vertical mini-histogram from a rolling history list (newest right).
    Bars are scaled against the window's own max -- fine for a NET sparkline
    where the point is "shape of traffic", not absolute value."""
    vals = [float(v) for v in list(vals)[-n:]]
    if len(vals) < n:
        vals = [0.0] * (n - len(vals)) + vals
    vmax = max(vals) or 1.0
    slot = w / n
    bar = slot * 0.72
    for i, v in enumerate(vals):
        x0 = x + i * slot + (slot - bar) / 2
        fh = int(round(h * (v / vmax))) if vmax > 0 else 0
        col = P["WHITE"] if v > 0 else P["DGRAY"]
        d.rectangle([x0, y + h - fh, x0 + bar, y + h], fill=col + (255,))


def _core_hist(d, x, y, w, h, cores, P, cap=16):
    """Per-core bars, TRUE scale (a 92% core is 92% of the bar height,
    never stretched to look like the max). Capped at `cap` columns; extra
    cores get a trailing '+k' note by the caller."""
    cores = [float(v) for v in cores[:cap]]
    n = max(1, len(cores))
    slot = w / cap
    bar = slot * 0.62
    for i in range(cap):
        x0 = x + i * slot + (slot - bar) / 2
        # track
        d.rectangle([x0, y, x0 + bar, y + h], fill=P["DGRAY"] + (255,))
        if i < n:
            fh = int(round(h * (min(100.0, max(0.0, cores[i])) / 100.0)))
            if fh > 0:
                d.rectangle([x0, y + h - fh, x0 + bar, y + h],
                            fill=P["WHITE"] + (255,))


def _chip(d, right_x, y, text, P, size=20, h=32, fill=False):
    """Right-aligned status chip. fill=True -> inverted (alarm style)."""
    f = _font(size)
    tw = d.textlength(text, font=f)
    x0 = right_x - tw - 20
    if fill:
        d.rectangle([x0, y, right_x, y + h], fill=P["WHITE"] + (255,))
        _text(d, (right_x - 10, y + h // 2), text, size, P["INK"],
              anchor="rm")
    else:
        d.rectangle([x0, y, right_x, y + h], fill=P["BG"] + (255,),
                    outline=P["WHITE"] + (255,), width=2)
        _text(d, (right_x - 10, y + h // 2), text, size, P["WHITE"],
              anchor="rm")


def _section_label(d, x, y, label, P, s):
    # marker = a small filled square drawn as a shape (NOT a glyph), so the
    # cover renders identically on any PC regardless of font coverage
    sz = int(16 * s)
    d.rectangle([x, y + int(6 * s), x + sz, y + int(6 * s) + sz],
                fill=P["WHITE"] + (255,))
    _text(d, (x + int(28 * s), y), label, int(30 * s), P["WHITE"],
          anchor="lt")
    d.line([(x, y + int(40 * s)), (x + int(600 * s), y + int(40 * s))],
           fill=P["DGRAY"] + (255,), width=1)
    return y + int(56 * s)


# ---- section renderers (design units @1080x1440, scaled by s) -------------

def _sec_cpu(d, x0, xr, y, s, st, P):
    cpu = st.get("cpu") or {}
    if not cpu:
        _text(d, (x0, y), "cpu: n/a", int(24 * s), P["MGRAY"])
        return y + int(40 * s)
    y = _section_label(d, x0, y, "CPU", P, s)
    _text(d, (x0, y), "%d%%" % int(round(cpu.get("pct", 0))),
          int(92 * s), P["WHITE"], anchor="lt")
    loads = "%.1f / %.1f / %.1f" % (cpu.get("load1", 0),
                                    cpu.get("load5", 0),
                                    cpu.get("load15", 0))
    _text(d, (xr, y + int(8 * s)), "LOAD  %s" % loads, int(24 * s),
          P["LGRAY"], anchor="rt")
    extra = "CORES %d" % cpu.get("n", 0)
    if cpu.get("tempC") is not None:
        extra += "   TEMP %d\u00b0C" % int(round(float(cpu["tempC"])))
    _text(d, (xr, y + int(38 * s)), extra, int(22 * s), P["MGRAY"],
          anchor="rt")
    y += int(108 * s)
    cores = [float(v) for v in (cpu.get("cores") or [])]
    if cores:
        bh = int(44 * s)
        _core_hist(d, x0, y, xr - x0, bh, cores, P)
        if len(cores) > 16:
            _text(d, (xr, y + int(4 * s)), "+%d cores hidden"
                  % (len(cores) - 16), int(16 * s), P["MGRAY"], anchor="rt")
        y += bh + int(24 * s)
    return y


def _sec_mem(d, x0, xr, y, s, st, P):
    mem = st.get("mem") or {}
    if not mem:
        return y
    y = _section_label(d, x0, y, "MEM", P, s)
    pct = float(mem.get("pct", 0))
    _segbar(d, x0, y, xr - x0, int(26 * s), pct, P)
    y += int(42 * s)
    _text(d, (x0, y),
          "%.1fG / %.1fG   %d%%" % (mem.get("usedGB", 0),
                                    mem.get("totalGB", 0), int(pct)),
          int(24 * s), P["LGRAY"], anchor="lt")
    swt = mem.get("swapTotalGB", 0) or 0
    if swt > 0:
        _text(d, (xr, y),
              "SWAP %.1f/%.1fG (%d%%)" % (mem.get("swapUsedGB", 0), swt,
                                          mem.get("swapPct", 0)),
              int(22 * s), P["MGRAY"], anchor="rt")
    return y + int(38 * s)


def _sec_disk(d, x0, xr, y, s, st, P):
    disks = st.get("disk") or []
    y = _section_label(d, x0, y, "DISK", P, s)
    if not disks:
        _text(d, (x0, y), "- none -", int(22 * s), P["MGRAY"])
        return y + int(34 * s)
    for drow in disks:
        # truncate the mount name to fit the fixed label column so it can
        # never collide with the bar (font-agnostic: measured, not counted)
        mnt = str(drow.get("mnt", "?"))
        tf = _font(int(22 * s))
        while mnt and d.textlength(mnt, font=tf) > int(92 * s):
            mnt = mnt[:-1]
        if not mnt:
            mnt = "?"
        pct = float(drow.get("pct", 0))
        rh = int(40 * s)
        _text(d, (x0, y + int(4 * s)), mnt, int(22 * s), P["LGRAY"],
              anchor="lt")
        _segbar(d, x0 + int(100 * s), y + int(11 * s), int(300 * s),
                 int(18 * s), pct, P, n=10)
        _text(d, (xr, y + int(4 * s)),
              "%.0f/%.0fG  %d%%" % (drow.get("usedGB", 0),
                                    drow.get("totalGB", 0), int(pct)),
              int(22 * s),
              P["WHITE"] if pct >= 90 else P["MGRAY"], anchor="rt")
        y += rh + int(8 * s)
    return y


def _sec_net(d, x0, xr, y, s, st, P):
    net = st.get("net") or {}
    y = _section_label(d, x0, y, "NET", P, s)
    _text(d, (x0, y), "DOWN %s KB/s     UP %s KB/s"
          % (net.get("downKbps", 0), net.get("upKbps", 0)),
          int(22 * s), P["LGRAY"], anchor="lt")
    y += int(36 * s)
    hist = net.get("hist_down") or []
    if hist:
        _spark(d, x0, y, xr - x0, int(52 * s), hist, P, n=60)
        y += int(52 * s + 26 * s)
    return y


def _sec_procs(d, x0, xr, y, s, st, P):
    procs = st.get("procs") or []
    y = _section_label(d, x0, y, "TOP PROCESSES", P, s)
    if not procs:
        _text(d, (x0, y), "- none -", int(22 * s), P["MGRAY"])
        return y + int(34 * s)
    _text(d, (x0 + int(170 * s), y - int(14 * s)), "CPU", int(18 * s),
          P["MGRAY"], anchor="lm")
    _text(d, (xr - int(130 * s), y - int(14 * s)), "MEM", int(18 * s),
          P["MGRAY"], anchor="lm")
    for p in procs[:5]:
        name = str(p.get("name", "?"))
        # clip the name to the name column so it can never run into the
        # CPU value column (measured, font-agnostic)
        tf = _font(int(22 * s))
        while name and d.textlength(name, font=tf) > int(150 * s):
            name = name[:-1]
        if not name:
            name = "?"
        _text(d, (x0, y), name, int(22 * s), P["LGRAY"], anchor="lt")
        _text(d, (x0 + int(170 * s), y), "%5s" % p.get("cpu", 0),
              int(20 * s), P["WHITE"], anchor="lt")
        _text(d, (xr - int(130 * s), y), "%8s" % p.get("memMB", 0),
              int(20 * s), P["WHITE"], anchor="lt")
        y += int(36 * s)
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

    # background grid
    step = max(24, int(96 * s))
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=P["DGRAY"] + (110,), width=1)
    for yy in range(0, h, step):
        d.line([(0, yy), (w, yy)], fill=P["DGRAY"] + (110,), width=1)

    m = int(30 * s)
    d.rectangle([m, m, w - m, h - m], outline=P["WHITE"] + (235,),
                width=max(2, int(6 * s)))
    d.rectangle([m + int(12 * s), m + int(12 * s),
                 w - m - int(12 * s), h - m - int(12 * s)],
                outline=P["LGRAY"] + (180,), width=2)
    bl = max(12, int(76 * s))
    for cx, cy in [(m, m), (w - m, m), (m, h - m), (w - m, h - m)]:
        _corner(d, cx, cy, 1 if cx == m else -1, 1 if cy == m else -1,
                bl, P["WHITE"])

    x0 = m + int(46 * s)
    xr = w - m - int(46 * s)
    bottom_limit = h - m - int(80 * s)
    y = m + int(44 * s)

    # ---- header --------------------------------------------------------------
    host = str(status.get("host", "?"))[:24]
    _text(d, (x0, y), "SYSTEM", int(60 * s), P["WHITE"], anchor="lt")
    cw = d.textlength(host, font=_font(int(26 * s)))
    d.rectangle([x0 + int(230 * s), y + int(14 * s),
                 x0 + int(230 * s) + cw + int(30 * s), y + int(54 * s)],
                fill=P["BG"] + (255,), outline=P["WHITE"] + (255,), width=2)
    _text(d, (x0 + int(245 * s), y + int(34 * s)), host, int(26 * s),
          P["WHITE"], anchor="lm")
    y += int(84 * s)
    _text(d, (x0, y), "// HOST RESOURCE MONITOR", int(22 * s), P["LGRAY"],
          anchor="lt")
    _text(d, (xr, y + int(4 * s)), "SYNC " + str(status.get("updatedAt", "?")),
          int(20 * s), P["MGRAY"], anchor="rt")
    if not status.get("live", True):
        _chip(d, xr, y + int(30 * s), "STALE", P, size=int(20 * s),
              h=int(30 * s), fill=True)
    y += int(56 * s)
    d.line([(m + int(12 * s), y), (w - m - int(12 * s), y)],
           fill=P["WHITE"] + (160,), width=2)
    y += int(34 * s)

    # ---- alarm strip (inverted full width = loudest on e-ink) ----------------
    alarms = list(status.get("alarms") or [])
    err = status.get("error")
    if err:
        alarms.append("ERR " + str(err)[:32])
    if alarms:
        msg = "  \u00b7  ".join(alarms[:3])
        if len(alarms) > 3:
            msg += "  \u00b7 +%d" % (len(alarms) - 3)
        while len(msg) > 40:
            msg = msg[:39]
        ay = y + 2
        d.rectangle([m + int(12 * s), ay, w - m - int(12 * s),
                     ay + int(44 * s)], fill=P["WHITE"] + (255,))
        # triangle is a polygon, not a glyph: renders identically on any PC
        tf = _font(int(22 * s))
        tw = d.textlength(msg, font=tf)
        tri = int(14 * s)
        gap = int(8 * s)
        cx = (x0 + xr) // 2
        ty = ay + int(22 * s)
        ux = cx - (tri + gap + tw) // 2
        d.polygon([(ux + tri // 2, ty - tri // 2),
                   (ux, ty + tri // 2),
                   (ux + tri, ty + tri // 2)], fill=P["INK"] + (255,))
        d.text((ux + tri + gap, ty), msg, font=tf,
               fill=P["INK"] + (255,), anchor="lm")
        y = ay + int(44 * s) + int(14 * s)

    # ---- sections; on a short screen the NET sparkline and TOP block are the
    # ---- first things dropped, never the gauges ------------------------------
    y = _sec_cpu(d, x0, xr, y, s, status, P)
    y = _sec_mem(d, x0, xr, y, s, status, P)
    y = _sec_disk(d, x0, xr, y, s, status, P)
    if y + int(140 * s) < bottom_limit:
        y = _sec_net(d, x0, xr, y, s, status, P)
    if y + int(200 * s) < bottom_limit:
        y = _sec_procs(d, x0, xr, y, s, status, P)

    # ---- footer ----------------------------------------------------------------
    fy = h - m - int(56 * s)
    d.line([(m + int(12 * s), fy - int(18 * s)),
            (w - m - int(12 * s), fy - int(18 * s))],
           fill=P["WHITE"] + (150,), width=2)
    up = status.get("uptime") or "?"
    _text(d, (x0, fy),
          "UP %s   SRC:%s" % (up, str(status.get("source", "?")).upper()),
          int(18 * s), P["MGRAY"], anchor="lt")
    if exit_hint:
        hx, hy = xr - int(14 * s), fy - int(12 * s)
        d.rectangle([hx - int(20 * s), hy - int(20 * s),
                     hx + int(20 * s), hy + int(20 * s)],
                    fill=P["WHITE"] + (255,))
        # "close" is two crossed lines, not a glyph: font-independent
        r = int(13 * s)
        wd = max(1, int(3 * s))
        d.line([(hx - r, hy - r), (hx + r, hy + r)],
               fill=P["INK"] + (255,), width=wd)
        d.line([(hx - r, hy + r), (hx + r, hy - r)],
               fill=P["INK"] + (255,), width=wd)

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
            {"name": "python3", "pid": 77, "cpu": 2.2, "memMB": 120.6},
            {"name": "xdg-desktop-portal", "pid": 551, "cpu": 0.4,
             "memMB": 38.1},
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
