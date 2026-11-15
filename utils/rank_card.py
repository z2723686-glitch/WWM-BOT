# -*- coding: utf-8 -*-
"""
WWM - Premium Rank Card

Layout (unchanged identity): avatar on the left with a status dot, username
and XP above a capsule progress bar, RANK / LEVEL top-right, soft watermark
bottom-right. Rendered at 2x and downscaled with LANCZOS.

Status colours are passed in from leveling.py based on the real member.status.
"""

import asyncio
import io
import logging
import math
import os
import shutil
import threading
import urllib.request

import aiohttp
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from utils.xp_system import format_xp

logger = logging.getLogger("wwm.rank_card")

# ── Canvas ────────────────────────────────────────────────────────────────────
W, H   = 960, 280
S      = 2
RW, RH = W * S, H * S

try:
    _LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1
    _LANCZOS = Image.LANCZOS

# ── Palette ───────────────────────────────────────────────────────────────────
CYAN      = (85, 214, 255)             # #55D6FF
CYAN_DEEP = (62, 183, 235)             # left end of the progress gradient
WHITE     = (255, 255, 255)
SOFT_WHITE = (236, 240, 246)
GRAY      = (160, 170, 186)
DIM       = (150, 162, 182)
STEEL     = (104, 148, 190)            # thin card / ring border colour
BAR_BG    = (255, 255, 255, 34)
BAR_EDGE  = (255, 255, 255, 28)
BAR_FILL  = (*CYAN, 255)
CARD_BG   = (10, 14, 24, 122)
OVERLAY   = (6, 9, 18, 138)
RING_RGBA = (10, 13, 22, 238)          # dark avatar ring / dot border

# ── Status colours ────────────────────────────────────────────────────────────
STATUS_ONLINE    = (67, 181, 129)   # green
STATUS_IDLE      = (250, 166, 26)   # yellow/amber
STATUS_DND       = (240, 71, 71)    # red
STATUS_OFFLINE   = (116, 127, 141)  # gray (invisible shows same as offline)

_STATUS_DEFAULTS = {
    "online":    STATUS_ONLINE,
    "idle":      STATUS_IDLE,
    "dnd":       STATUS_DND,
    "offline":   STATUS_OFFLINE,
    "invisible": STATUS_OFFLINE,
}

BG_URL = "https://i.ibb.co/1f6rj5Rx/b67f5d4399.jpg"
MAX_IMAGE_BYTES = 15 * 1024 * 1024

# ── Fonts ─────────────────────────────────────────────────────────────────────
FONTS_DIR   = os.path.join(os.path.dirname(__file__), "..", "assets", "fonts")
BOLD_PATH   = os.path.join(FONTS_DIR, "bold.ttf")
POPPINS_URL = "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-{}.ttf"

_WEIGHT_NAMES = {"bold": "Bold", "semibold": "SemiBold", "medium": "Medium"}
_WEIGHT_CHAIN = {
    "bold":     ("bold",),
    "semibold": ("semibold", "medium", "bold"),
    "medium":   ("medium", "semibold", "bold"),
}
_POPPINS_DIRS = [
    "/usr/share/fonts/truetype/google-fonts",
    "/usr/share/fonts/truetype/poppins",
    "/usr/share/fonts/poppins",
    "/usr/local/share/fonts",
]
SYSFONTS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

_font_paths = {}
_font_lock = threading.Lock()


def _valid_font(path):
    try:
        return os.path.exists(path) and os.path.getsize(path) > 10000
    except OSError:
        return False


def _locate_poppins(weight):
    """Find (or fetch) Poppins in the given weight. Returns a path or None."""
    name  = _WEIGHT_NAMES[weight]
    local = os.path.join(FONTS_DIR, weight + ".ttf")
    if _valid_font(local):
        return local

    for folder in _POPPINS_DIRS:
        src = os.path.join(folder, "Poppins-{}.ttf".format(name))
        if _valid_font(src):
            try:
                os.makedirs(FONTS_DIR, exist_ok=True)
                shutil.copy(src, local)
                if _valid_font(local):
                    return local
            except Exception:
                pass
            return src

    try:
        os.makedirs(FONTS_DIR, exist_ok=True)
        tmp = local + ".part"
        with urllib.request.urlopen(POPPINS_URL.format(name), timeout=10) as r, \
                open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh)
        if _valid_font(tmp):
            os.replace(tmp, local)
            return local
        os.remove(tmp)
    except Exception as e:
        logger.error("Font download failed (%s): %s", name, e)
    return None


def _poppins(weight):
    with _font_lock:
        if weight not in _font_paths:
            _font_paths[weight] = _locate_poppins(weight)
        return _font_paths[weight]


def _generic_font():
    for p in SYSFONTS:
        if os.path.exists(p):
            return p
    return None


def _resolve_font(weight):
    for w in _WEIGHT_CHAIN[weight]:
        p = _poppins(w)
        if p:
            return p
    return _generic_font()


def _ensure_font():
    """Kept for compatibility: returns a usable bold font path (or None)."""
    return _resolve_font("bold")


def _font(weight, size):
    """Font at logical `size` (scaled by the 2x supersampling factor)."""
    px   = max(1, int(round(size * S)))
    path = _resolve_font(weight)
    if path:
        try:
            return ImageFont.truetype(path, px)
        except Exception as e:
            logger.warning("font load failed %s: %s", path, e)
    try:
        return ImageFont.load_default(px)
    except TypeError:
        return ImageFont.load_default()


def _f(size):
    return _font("bold", size)


def _fn(size):
    return _font("medium", size)


# ── Text helpers ──────────────────────────────────────────────────────────────

def _tw(font, text):
    try:
        return int(round(font.getlength(text)))
    except Exception:
        return len(text) * max(8, getattr(font, "size", 16) // 2)


def _text_w(font, text, tracking=0):
    if not text:
        return 0
    return _tw(font, text) + tracking * (len(text) - 1)


def _metrics(font):
    try:
        asc, desc = font.getmetrics()
        return int(asc), int(desc)
    except Exception:
        size = getattr(font, "size", 16)
        return int(size), int(size * 0.25)


def _cap_h(font):
    """Height of a capital letter / lining digit in pixels."""
    try:
        box = font.getbbox("H", anchor="ls")
        return max(1, -box[1])
    except Exception:
        return int(getattr(font, "size", 16) * 0.7)


def _put(draw, x, y, text, font, fill):
    try:
        draw.text((int(x), int(y)), text, fill=fill, font=font, anchor="ls")
    except (ValueError, TypeError, OSError):
        asc, _ = _metrics(font)
        draw.text((int(x), int(y - asc)), text, fill=fill, font=font)


def _text(draw, x, y, text, font, fill, align="l", tracking=0):
    """Draw `text` with its baseline at y. align 'r' makes x the right edge."""
    if not text:
        return 0
    w  = _text_w(font, text, tracking)
    x0 = x - w if align == "r" else x
    if tracking:
        cx = x0
        for ch in text:
            _put(draw, cx, y, ch, font, fill)
            cx += _tw(font, ch) + tracking
    else:
        _put(draw, x0, y, text, font, fill)
    return w


def _faint_text(cvs, x_right, y, text, font, rgb, alpha, tracking=0):
    """Right-aligned translucent text, composited without dark edge fringes."""
    w        = _text_w(font, text, tracking)
    asc, dsc = _metrics(font)
    pad      = 4 * S
    tile     = Image.new("RGBA", (w + pad * 2, asc + dsc + pad * 2), (*rgb, 0))
    _text(ImageDraw.Draw(tile), pad, pad + asc, text, font, (*rgb, 255), "l", tracking)
    tile.putalpha(tile.getchannel("A").point(lambda v: v * alpha // 255))
    cvs.alpha_composite(tile, dest=(int(x_right - w - pad), int(y - asc - pad)))


def _fit_text(font, text, max_w, ell="\u2026"):
    """Truncate with an ellipsis so the text fits into max_w pixels."""
    if _tw(font, text) <= max_w:
        return text
    lo, hi, best = 1, len(text), ""
    while lo <= hi:
        mid  = (lo + hi) // 2
        cand = text[:mid].rstrip() + ell
        if _tw(font, cand) <= max_w:
            best, lo = cand, mid + 1
        else:
            hi = mid - 1
    return best or ell


def _clean_name(name):
    s = "".join(ch if ch.isprintable() else " " for ch in str(name or ""))
    s = " ".join(s.split())
    return s or "Unknown"


# ── Value helpers ─────────────────────────────────────────────────────────────

def _as_int(v, default=0):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return int(f)
    except (TypeError, ValueError):
        return default


def _rgb(c, default):
    try:
        vals = [int(max(0, min(255, int(v)))) for v in list(c)[:3]]
        if len(vals) == 3:
            return tuple(vals)
    except Exception:
        pass
    return default


def _norm_status(s):
    s = getattr(s, "value", s)
    s = str(s or "offline").strip().lower()
    if s in ("do_not_disturb", "do not disturb"):
        return "dnd"
    return s


# ── Image helpers ─────────────────────────────────────────────────────────────

def _circle_mask(sz):
    """Anti-aliased circular mask (drawn at 4x, reduced with LANCZOS)."""
    ss = 4
    m  = Image.new("L", (sz * ss, sz * ss), 0)
    ImageDraw.Draw(m).ellipse([0, 0, sz * ss - 1, sz * ss - 1], fill=255)
    return m.resize((sz, sz), _LANCZOS)


def _decode(data):
    return Image.open(io.BytesIO(data)).convert("RGBA")


async def _fetch(s, url):
    try:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                data = await r.read()
                if len(data) > MAX_IMAGE_BYTES:
                    logger.warning("fetch %s: image too large", url)
                    return None
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, _decode, data)
    except Exception as e:
        logger.warning("fetch %s: %s", url, e)
    return None


def _avatar_url(url):
    if "size=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "size=512"


def _gradient_strip(w, h, c0, c1):
    strip = Image.new("RGBA", (w, 1))
    px    = strip.load()
    span  = max(1, w - 1)
    for x in range(w):
        t = x / span
        px[x, 0] = (
            int(c0[0] + (c1[0] - c0[0]) * t),
            int(c0[1] + (c1[1] - c0[1]) * t),
            int(c0[2] + (c1[2] - c0[2]) * t),
            255,
        )
    return strip.resize((w, h), Image.NEAREST)


def _status_badge(status, color, r, border, edge):
    """Status dot with a dark border, drawn at 4x and reduced for clean edges."""
    ss    = 4
    size  = (r + border) * 2
    big   = size * ss
    c     = big // 2
    R     = r * ss
    B     = (r + border) * ss

    def disc(cx, cy, rad):
        m = Image.new("L", (big, big), 0)
        ImageDraw.Draw(m).ellipse([cx - rad, cy - rad, cx + rad - 1, cy + rad - 1], fill=255)
        return m

    outer = disc(c, c, B)
    inner = disc(c, c, R)
    body  = inner

    if status == "idle":                      # crescent moon
        cut  = disc(c + int(R * 0.42), c - int(R * 0.42), int(R * 0.72))
        body = ImageChops.subtract(inner, cut)
    elif status in ("invisible", "offline"):  # hollow ring
        body = ImageChops.subtract(inner, disc(c, c, int(R * 0.5)))

    tile = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    tile.paste(edge, None, outer)             # border + any cut-out areas
    tile.paste((*color, 255), None, body)

    if status == "dnd":                       # horizontal bar
        bar_h = max(2, int(R * 0.34))
        pad   = int(R * 0.30)
        ImageDraw.Draw(tile).rounded_rectangle(
            [c - R + pad, c - bar_h // 2, c + R - pad, c + bar_h // 2],
            radius=bar_h // 2, fill=(255, 255, 255, 255))

    return tile.resize((size, size), _LANCZOS)


# ── Public entry ──────────────────────────────────────────────────────────────

async def generate_rank_card_with_avatar(
    username, discriminator, avatar_url,
    level, current_xp, needed_xp, total_xp,
    rank, messages, voice_minutes,
    status_color=STATUS_OFFLINE,
    status_name="offline",            # "online"|"idle"|"dnd"|"offline"|"invisible"
    background_url=None,
) -> io.BytesIO:
    bg_img = av_img = None
    async with aiohttp.ClientSession() as s:
        bg_task = _fetch(s, background_url) if background_url else None
        av_task = _fetch(s, _avatar_url(avatar_url)) if avatar_url else None
        tasks   = [t for t in (bg_task, av_task) if t is not None]
        results = iter(await asyncio.gather(*tasks)) if tasks else iter(())
        if bg_task is not None:
            bg_img = next(results)
        if av_task is not None:
            av_img = next(results)
        if bg_img is None:                # custom background failed -> default
            bg_img = await _fetch(s, BG_URL)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _render,
        bg_img, av_img, username, level, current_xp, needed_xp,
        total_xp, rank, messages, voice_minutes, status_color, status_name,
    )


# ── Renderer ──────────────────────────────────────────────────────────────────

def _render(bg_img, av_img, username, level, current_xp, needed_xp,
            total_xp, rank, messages, voice_minutes, status_color, status_name):

    name_str = _clean_name(username)
    cur      = max(0, _as_int(current_xp))
    need     = max(0, _as_int(needed_xp))
    prog     = 0.0 if need <= 0 else min(1.0, max(0.0, cur / need))
    status   = _norm_status(status_name)
    dot_rgb  = _rgb(status_color, _STATUS_DEFAULTS.get(status, STATUS_ONLINE))
    rank_txt = "#{}".format(rank if rank is not None else "?")
    lvl_txt  = str(level if level is not None else 0)

    # ── Background: cover-scale, centre-crop, subtle blur, dark overlay ───
    cvs = Image.new("RGBA", (RW, RH), (18, 18, 30, 255))
    if bg_img is not None:
        try:
            flat = Image.new("RGBA", bg_img.size, (18, 18, 30, 255))
            flat.alpha_composite(bg_img.convert("RGBA"))
            cvs = ImageOps.fit(flat.convert("RGB"), (RW, RH), _LANCZOS,
                               centering=(0.5, 0.5)).convert("RGBA")
        except Exception as e:
            logger.warning("background render failed: %s", e)
    cvs = cvs.filter(ImageFilter.GaussianBlur(1.5 * S))
    cvs.alpha_composite(Image.new("RGBA", (RW, RH), OVERLAY))

    # ── Glass card ────────────────────────────────────────────────────────
    H_MARGIN = int(RW * 0.04)
    V_MARGIN = int(RH * 0.12)
    CR = 26 * S
    CX1, CY1 = H_MARGIN, V_MARGIN
    CX2, CY2 = RW - H_MARGIN, RH - V_MARGIN
    CW, CH   = CX2 - CX1, CY2 - CY1

    card_mask = Image.new("L", (CW, CH), 0)
    ImageDraw.Draw(card_mask).rounded_rectangle([0, 0, CW - 1, CH - 1], radius=CR, fill=255)

    # frosted look: extra blur only behind the card
    frost = cvs.crop((CX1, CY1, CX2, CY2)).filter(ImageFilter.GaussianBlur(4 * S))
    cvs.paste(frost, (CX1, CY1), card_mask)

    tint = Image.new("RGBA", (CW, CH), CARD_BG[:3] + (255,))
    tint.putalpha(card_mask.point(lambda v: v * CARD_BG[3] // 255))
    cvs.alpha_composite(tint, dest=(CX1, CY1))

    border = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle(
        [CX1, CY1, CX2 - 1, CY2 - 1], radius=CR,
        outline=(*STEEL, 150), width=S)
    cvs.alpha_composite(border)

    draw = ImageDraw.Draw(cvs)

    # ── Layout ────────────────────────────────────────────────────────────
    RING_PAD = 6 * S
    AV_SZ    = 164 * S
    AV_X     = CX1 + 22 * S + RING_PAD
    AV_Y     = CY1 + (CH - AV_SZ) // 2
    LZ_X     = AV_X + AV_SZ + RING_PAD + 24 * S
    RZ_RIGHT = CX2 - 26 * S

    BAR_H = 34 * S
    BAR_R = BAR_H // 2
    BAR_X = LZ_X
    BAR_W = RZ_RIGHT - LZ_X
    BAR_Y = CY2 - 28 * S - BAR_H

    # ── RANK + LEVEL (top right, right-aligned columns) ───────────────────
    f_lbl    = _font("medium", 12)
    LBL_TRK  = int(1.6 * S)
    GAP      = 34 * S
    max_total = min(RZ_RIGHT - LZ_X - 20 * S, 380 * S)

    size = 48
    while True:
        f_num = _font("bold", size)
        rank_w = max(_tw(f_num, rank_txt), _text_w(f_lbl, "RANK", LBL_TRK))
        lvl_w  = max(_tw(f_num, lvl_txt),  _text_w(f_lbl, "LEVEL", LBL_TRK))
        if rank_w + lvl_w + GAP <= max_total or size <= 26:
            break
        size -= 2

    LABEL_BASE = CY1 + 36 * S
    NUM_BASE   = LABEL_BASE + 10 * S + _cap_h(f_num)
    LVL_RIGHT  = RZ_RIGHT
    RANK_RIGHT = RZ_RIGHT - lvl_w - GAP

    _text(draw, RANK_RIGHT, LABEL_BASE, "RANK",  f_lbl, GRAY, "r", LBL_TRK)
    _text(draw, RANK_RIGHT, NUM_BASE,   rank_txt, f_num, WHITE, "r")
    _text(draw, LVL_RIGHT,  LABEL_BASE, "LEVEL", f_lbl, GRAY, "r", LBL_TRK)
    _text(draw, LVL_RIGHT,  NUM_BASE,   lvl_txt,  f_num, CYAN, "r")

    # ── XP text (right) + username (left) on one baseline above the bar ───
    ROW_BASE = BAR_Y - 16 * S
    f_xp     = _font("medium", 16)
    xp_cur   = format_xp(cur)
    xp_rest  = " / {} XP".format(format_xp(need))
    rest_w   = _tw(f_xp, xp_rest)
    cur_w    = _tw(f_xp, xp_cur)
    xp_w     = cur_w + rest_w
    _text(draw, BAR_X + BAR_W,          ROW_BASE, xp_rest, f_xp, GRAY,       "r")
    _text(draw, BAR_X + BAR_W - rest_w, ROW_BASE, xp_cur,  f_xp, SOFT_WHITE, "r")

    name_max = max(80 * S, BAR_W - xp_w - 24 * S)
    for nsize in (30, 28, 26):
        f_name = _font("semibold", nsize)
        if _tw(f_name, name_str) <= name_max:
            break
    name_str = _fit_text(f_name, name_str, name_max)
    _text(draw, LZ_X, ROW_BASE, name_str, f_name, WHITE, "l")

    # ── Progress bar (capsule, subtle cyan gradient, clamped 0-100%) ──────
    blyr = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
    bd   = ImageDraw.Draw(blyr)
    bd.rounded_rectangle([BAR_X, BAR_Y, BAR_X + BAR_W - 1, BAR_Y + BAR_H - 1],
                         radius=BAR_R, fill=BAR_BG, outline=BAR_EDGE, width=1)
    if prog > 0:
        fill_w = min(BAR_W, max(BAR_H, int(round(BAR_W * prog))))
        grad   = _gradient_strip(fill_w, BAR_H, CYAN_DEEP, CYAN)
        fmask  = Image.new("L", (fill_w, BAR_H), 0)
        ImageDraw.Draw(fmask).rounded_rectangle(
            [0, 0, fill_w - 1, BAR_H - 1], radius=BAR_R, fill=255)
        grad.putalpha(fmask)
        blyr.alpha_composite(grad, dest=(BAR_X, BAR_Y))
    cvs.alpha_composite(blyr)

    # ── Watermark ─────────────────────────────────────────────────────────
    _faint_text(cvs, BAR_X + BAR_W, CY2 - 10 * S, "WIND HUNTED UNIVERSE",
                _font("medium", 10), DIM, 92, tracking=int(1.8 * S))

    # ── Avatar + thin dark ring ───────────────────────────────────────────
    ring_sz = AV_SZ + RING_PAD * 2
    ring    = Image.new("RGBA", (ring_sz, ring_sz), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse([0, 0, ring_sz - 1, ring_sz - 1],
                                 fill=RING_RGBA, outline=(*STEEL, 120), width=S)
    cvs.alpha_composite(ring, dest=(AV_X - RING_PAD, AV_Y - RING_PAD))

    mask = _circle_mask(AV_SZ)
    if av_img is not None:
        try:
            av = ImageOps.fit(av_img.convert("RGBA"), (AV_SZ, AV_SZ), _LANCZOS)
        except Exception:
            av = None
    else:
        av = None
    if av is None:
        av = Image.new("RGBA", (AV_SZ, AV_SZ), (45, 50, 68, 255))
        initial = name_str[:1].upper()
        if initial.isascii() and initial.isalnum():
            f_ini = _font("bold", 64)
            _text(ImageDraw.Draw(av), AV_SZ // 2 - _tw(f_ini, initial) // 2,
                  AV_SZ // 2 + _cap_h(f_ini) // 2, initial, f_ini, (120, 130, 152, 255))
    av.putalpha(ImageChops.multiply(av.getchannel("A"), mask))
    cvs.alpha_composite(av, dest=(AV_X, AV_Y))

    # ── Status dot: on the avatar edge, bottom-right ──────────────────────
    DOT_R, DOT_B = 14 * S, 4 * S
    badge  = _status_badge(status, dot_rgb, DOT_R, DOT_B, RING_RGBA[:3] + (255,))
    centre = AV_SZ / 2 + RING_PAD * 0.5
    off    = centre * math.sqrt(0.5)
    bx     = int(AV_X + AV_SZ / 2 + off - badge.width / 2)
    by     = int(AV_Y + AV_SZ / 2 + off - badge.height / 2)
    cvs.alpha_composite(badge, dest=(bx, by))

    # ── Final downscale ───────────────────────────────────────────────────
    out = cvs.resize((W, H), _LANCZOS)
    buf = io.BytesIO()
    out.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
