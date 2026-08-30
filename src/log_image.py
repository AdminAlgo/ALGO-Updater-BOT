"""Render a driver's HOS "log card" image — the 4 ring gauges (Break / Drive /
Shift / Cycle) plus name, duty status, and truck — mirroring the DriveHOS
dashboard header. Generated from live data (no dashboard login needed).

Returns PNG bytes, ready to attach to a Telegram photo message.
"""

from __future__ import annotations

import io
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from .eld.base import DriverSnapshot

# FMCSA full limits (seconds) — used as each ring's "full" denominator.
LIMITS = {"break": 8 * 3600, "drive": 11 * 3600, "shift": 14 * 3600, "cycle": 70 * 3600}

# Ring colors (match the dashboard).
COLORS = {
    "break": (214, 137, 16),    # amber/orange
    "drive": (30, 132, 73),     # green
    "shift": (36, 113, 163),    # blue
    "cycle": (125, 60, 152),    # purple
}
TRACK = (230, 230, 232)         # faint background ring
INK = (33, 37, 41)
MUTED = (120, 125, 130)

# Duty-status badge colors. The bot only sends alerts for active statuses, so
# only these are styled: Driving = green, On Duty / Yard Move = blue.
# (Off Duty / Sleeper / PC never get a card — drivers resting get no alerts.)
BADGE = {
    "Driving": (39, 174, 96),        # green
    "On Duty": (41, 128, 185),       # blue
    "Yard Move": (41, 128, 185),     # blue
}
STATUS_ABBR = {
    "Driving": "DR", "On Duty": "ON", "Yard Move": "YM",
    "Sleeper": "SB", "Off Duty": "OFF", "Personal Conveyance": "PC",
}

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    # No TrueType font found — use a SCALABLE default so text stays legible
    # (Pillow >= 10 supports load_default(size=...)); older Pillow ignores size.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _hhmm(seconds: int) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"


SIGNAL_GREEN = (39, 174, 96)
BT_BLUE = (36, 113, 163)
BT_RED = (214, 48, 49)
GREY = (160, 165, 170)


def _draw_signal(d, x, y, size, color):
    """Four ascending cellular bars, bottom-aligned."""
    bars = 4
    bw = size / (bars * 1.7)
    gap = bw * 0.7
    for i in range(bars):
        bh = size * (0.4 + 0.6 * i / (bars - 1))
        bx = x + i * (bw + gap)
        d.rounded_rectangle([bx, y + size - bh, bx + bw, y + size],
                            radius=max(1, int(bw * 0.25)), fill=color)


def _draw_bluetooth(d, x, y, size, color, slashed=False):
    """Bluetooth rune; with a diagonal slash when disconnected."""
    cx = x + size / 2
    ty, by, cy = y, y + size, y + size / 2
    r = size * 0.26
    qu, qd = cy - size / 4, cy + size / 4
    lx, rx = cx - r, cx + r
    w = max(2, int(size * 0.10))
    pts = [(lx, qu), (rx, qd), (cx, by), (cx, ty), (rx, qu), (lx, qd)]
    d.line(pts, fill=color, width=w, joint="curve")
    if slashed:
        d.line([(x + size * 0.08, y + size * 0.92), (x + size * 0.92, y + size * 0.08)],
               fill=color, width=w)


def _draw_ring(draw, cx, cy, r, key, seconds, scale):
    width = max(6, int(8 * scale))
    bbox = [cx - r, cy - r, cx + r, cy + r]
    draw.arc(bbox, 0, 360, fill=TRACK, width=width)
    frac = max(0.0, min(1.0, seconds / LIMITS[key]))
    if frac > 0:
        draw.arc(bbox, -90, -90 + 360 * frac, fill=COLORS[key], width=width)
    t_font = _font(int(22 * scale))
    l_font = _font(int(15 * scale))
    draw.text((cx, cy - int(8 * scale)), _hhmm(seconds), font=t_font, fill=INK, anchor="mm")
    draw.text((cx, cy + int(16 * scale)), key.capitalize(), font=l_font,
              fill=COLORS[key], anchor="mm")


def render_hours_card(snap: DriverSnapshot, scale: float = 2.0,
                      disconnected: bool = False) -> bytes:
    """Render the driver's HOS ring card to PNG bytes.

    When ``disconnected`` is True (device OFFLINE/stale while we care), the
    Bluetooth icon is drawn red with a slash and the signal bars greyed.
    """
    W, H = int(1100 * scale), int(240 * scale)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    pad = int(40 * scale)
    label = snap.duty_status_label

    # --- left block: name, status badge, truck ---
    name_font = _font(int(30 * scale))
    d.text((pad, int(55 * scale)), snap.name, font=name_font, fill=INK, anchor="lm")

    # status badge to the right of the name
    name_w = d.textlength(snap.name, font=name_font)
    badge_font = _font(int(16 * scale))
    abbr = STATUS_ABBR.get(label, label[:3].upper())
    bx = pad + int(name_w) + int(18 * scale)
    bw, bh = int(48 * scale), int(26 * scale)
    by = int(42 * scale)
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=int(6 * scale),
                        fill=BADGE.get(label, (149, 165, 166)))
    d.text((bx + bw / 2, by + bh / 2), abbr, font=badge_font, fill="white", anchor="mm")

    # connection icons (signal + bluetooth) just right of the badge
    icon_sz = int(24 * scale)
    iy = by + (bh - icon_sz) // 2
    ix = bx + bw + int(18 * scale)
    has_veh = snap.connection.has_vehicle
    _draw_signal(d, ix, iy, icon_sz,
                 GREY if (disconnected or not has_veh) else SIGNAL_GREEN)
    bt_x = ix + icon_sz + int(12 * scale)
    if not has_veh:
        _draw_bluetooth(d, bt_x, iy, icon_sz, GREY, slashed=True)
    elif disconnected:
        _draw_bluetooth(d, bt_x, iy, icon_sz, BT_RED, slashed=True)
    else:
        _draw_bluetooth(d, bt_x, iy, icon_sz, BT_BLUE, slashed=False)

    truck = snap.connection.vehicle_number
    info_font = _font(int(18 * scale))
    truck_txt = f"Truck: {truck}" if truck else "Truck: —"
    d.text((pad, int(110 * scale)), truck_txt, font=info_font, fill=(36, 113, 163), anchor="lm")
    d.text((pad, int(145 * scale)), label, font=info_font, fill=MUTED, anchor="lm")

    # --- right block: four rings ---
    r = int(48 * scale)
    gap = int(150 * scale)
    cy = H // 2
    first_cx = W - pad - r - gap * 3
    for i, key in enumerate(("break", "drive", "shift", "cycle")):
        cx = first_cx + gap * i
        secs = getattr(snap.hos, f"{key}_seconds")
        _draw_ring(d, cx, cy, r, key, secs, scale)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
