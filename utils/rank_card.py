# -*- coding: utf-8 -*-
"""
WWM - Premium Rank Card
Status dot: inside the black avatar ring, bottom-right corner.
Colours passed in from leveling.py based on real member.status.
"""

import asyncio
import io
import logging
import os
import shutil
import urllib.request
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from utils.xp_system import calculate_progress_percentage, format_xp, format_voice_time

logger = logging.getLogger("wwm.rank_card")

W, H   = 960, 280
S      = 2
RW, RH = W * S, H * S

CYAN      = (85, 214, 255)
WHITE     = (255, 255, 255)
GRAY      = (170, 170, 185)
DIM       = (110, 110, 125)
BAR_BG    = (255, 255, 255, 50)
BAR_FILL  = (85, 214, 255, 240)
CARD_BG   = (0, 0, 0, 115)
OVERLAY   = (8, 8, 18, 100)

# ── Status colours ────────────────────────────────────────────────────────────
STATUS_ONLINE    = (67, 181, 129)   # green
STATUS_IDLE      = (250, 166, 26)   # yellow/amber
STATUS_DND       = (240, 71, 71)    # red
STATUS_OFFLINE   = (116, 127, 141)  # gray (invisible shows same as offline)

BG_URL = "https://i.ibb.co/1f6rj5Rx/b67f5d4399.jpg"

FONTS_DIR   = os.path.join(os.path.dirname(__file__), "..", "assets", "fonts")
BOLD_PATH   = os.path.join(FONTS_DIR, "bold.ttf")
POPPINS_URL = "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf"
SYSFONTS    = [
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

def _ensure_font():
    if os.path.exists(BOLD_PATH) and os.path.getsize(BOLD_PATH) > 10000:
        return BOLD_PATH
    for p in SYSFONTS:
        if os.path.exists(p):
            os.makedirs(FONTS_DIR, exist_ok=True)
            try: shutil.copy(p, BOLD_PATH)
            except Exception: pass
            return BOLD_PATH if os.path.exists(BOLD_PATH) else p
    os.makedirs(FONTS_DIR, exist_ok=True)
    try:
        urllib.request.urlretrieve(POPPINS_URL, BOLD_PATH)
        if os.path.getsize(BOLD_PATH) > 10000:
            return BOLD_PATH
    except Exception as e:
        logger.error("Font download failed: %s", e)
    return None

def _f(size):
    p = _ensure_font()
    if p:
        try: return ImageFont.truetype(p, size * S)
        except Exception: pass
    return ImageFont.load_default()

def _fn(size):
    reg = os.path.join(FONTS_DIR, "regular.ttf")
    if os.path.exists(reg) and os.path.getsize(reg) > 10000:
        try: return ImageFont.truetype(reg, size * S)
        except Exception: pass
    return _f(size)

def _tw(font, text):
    try: return int(font.getlength(text))
    except Exception: return len(text) * max(8, font.size // 2)

def _circle_mask(sz):
    m = Image.new("L", (sz, sz), 0)
    ImageDraw.Draw(m).ellipse([0, 0, sz - 1, sz - 1], fill=255)
    return m

async def _fetch(s, url):
    try:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                return Image.open(io.BytesIO(await r.read())).convert("RGBA")
    except Exception as e:
        logger.warning("fetch %s: %s", url, e)
    return None

# ── Public entry ──────────────────────────────────────────────────────────────

async def generate_rank_card_with_avatar(
    username, discriminator, avatar_url,
    level, current_xp, needed_xp, total_xp,
    rank, messages, voice_minutes,
    status_color=STATUS_OFFLINE,
    status_name="offline",            # "online"|"idle"|"dnd"|"offline"|"invisible"
    background_url=None,
) -> io.BytesIO:
    _ensure_font()
    bg_img = av_img = None
    async with aiohttp.ClientSession() as s:
        bg_img = await _fetch(s, background_url or BG_URL)
        if avatar_url:
            av_img = await _fetch(s, avatar_url + "?size=256")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _render,
        bg_img, av_img, username, level, current_xp, needed_xp,
        total_xp, rank, messages, voice_minutes, status_color, status_name,
    )

# ── Renderer ──────────────────────────────────────────────────────────────────

def _render(bg_img, av_img, username, level, current_xp, needed_xp,
            total_xp, rank, messages, voice_minutes, status_color, status_name):

    # ── Background ────────────────────────────────────────────────────────
    if bg_img:
        bw, bh = bg_img.size
        sc = min(RW / bw, RH / bh)
        nb = bg_img.resize((int(bw * sc), int(bh * sc)), Image.LANCZOS)
        bg = Image.new("RGBA", (RW, RH), (18, 18, 30, 255))
        bg.paste(nb, ((RW - nb.width) // 2, (RH - nb.height) // 2))
    else:
        bg = Image.new("RGBA", (RW, RH), (18, 18, 30))
    bg  = bg.filter(ImageFilter.GaussianBlur(1 * S))
    cvs = bg.convert("RGBA")
    cvs = Image.alpha_composite(cvs, Image.new("RGBA", (RW, RH), OVERLAY))

    # ── Glass card ────────────────────────────────────────────────────────
    H_MARGIN = int(RW * 0.04)
    V_MARGIN = int(RH * 0.12)
    CR = 28 * S
    CX1, CY1 = H_MARGIN, V_MARGIN
    CX2, CY2 = RW - H_MARGIN, RH - V_MARGIN
    CW, CH   = CX2 - CX1, CY2 - CY1

    card = Image.new("RGBA", (CW, CH), CARD_BG)
    d    = ImageDraw.Draw(card)
    for i in range(4):
        a = int(90 * (1 - i / 4))
        d.rounded_rectangle([i, i, CW-1-i, CH-1-i], radius=max(1, CR-i),
                             outline=(*CYAN, a), width=1)
    mask = Image.new("L", (CW, CH), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, CW-1, CH-1], radius=CR, fill=255)
    layer = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
    layer.paste(card, (CX1, CY1), mask)
    cvs = Image.alpha_composite(cvs, layer)
    draw = ImageDraw.Draw(cvs)

    # ── Layout ────────────────────────────────────────────────────────────
    RZ_W     = 240 * S
    RZ_RIGHT = CX2 - 22 * S
    RZ_LEFT  = RZ_RIGHT - RZ_W
    AV_SZ    = 175 * S
    AV_X     = CX1 + 20 * S
    AV_Y     = (RH - AV_SZ) // 2
    LZ_X     = AV_X + AV_SZ + 20 * S
    LZ_W     = RZ_LEFT - LZ_X

    # ── Fonts ──────────────────────────────────────────────────────────────
    f_lbl  = _f(15)
    f_rnum = _f(54)
    f_lnum = _f(54)
    f_name = _fn(34)
    f_xp   = _f(17)
    f_wm   = _f(11)

    # ── RANK + LEVEL (right zone) ──────────────────────────────────────────
    HALF    = (RZ_W - 20 * S) // 2
    R_RIGHT = RZ_LEFT + HALF
    L_RIGHT = RZ_RIGHT
    TOP_Y   = CY1 + 20 * S
    NUM_Y   = TOP_Y + 22 * S

    draw.text((R_RIGHT - _tw(f_lbl,  "RANK"),               TOP_Y), "RANK",           fill=GRAY,  font=f_lbl)
    draw.text((R_RIGHT - _tw(f_rnum, "#{}".format(rank)),   NUM_Y), "#{}".format(rank), fill=WHITE, font=f_rnum)
    draw.text((L_RIGHT - _tw(f_lbl,  "LEVEL"),              TOP_Y), "LEVEL",          fill=GRAY,  font=f_lbl)
    draw.text((L_RIGHT - _tw(f_lnum, str(level)),           NUM_Y), str(level),       fill=CYAN,  font=f_lnum)

    # ── Bar ────────────────────────────────────────────────────────────────
    BAR_H = 42 * S
    BAR_R = BAR_H // 2
    BAR_Y = CY2 - BAR_H - 20 * S
    BAR_X = LZ_X
    BAR_W = CX2 - 22 * S - LZ_X

    # ── Username — above bar ───────────────────────────────────────────────
    name = username
    while name and _tw(f_name, name) > LZ_W:
        name = name[:-1]
    if name != username:
        name = name[:-2] + ".."

    try:
        bbox   = f_name.getbbox(name)
        name_h = bbox[3] - bbox[1]
    except Exception:
        name_h = int(f_name.size * 1.1)

    NAME_GAP = 28 * S
    NAME_Y   = BAR_Y - NAME_GAP - name_h
    draw.text((LZ_X, NAME_Y), name, fill=WHITE, font=f_name)

    # ── XP text ───────────────────────────────────────────────────────────
    xp_str = "{} / {} XP".format(format_xp(current_xp), format_xp(needed_xp))
    draw.text(
        (BAR_X + BAR_W - _tw(f_xp, xp_str), BAR_Y - 26 * S),
        xp_str, fill=GRAY, font=f_xp,
    )

    # ── Progress bar ──────────────────────────────────────────────────────
    prog   = calculate_progress_percentage(current_xp, needed_xp)
    fill_w = max(BAR_R * 2, int(BAR_W * prog))

    blyr = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
    bd   = ImageDraw.Draw(blyr)
    bd.rounded_rectangle([BAR_X, BAR_Y, BAR_X + BAR_W, BAR_Y + BAR_H], radius=BAR_R, fill=BAR_BG)
    bd.rounded_rectangle([BAR_X, BAR_Y, BAR_X + fill_w, BAR_Y + BAR_H], radius=BAR_R, fill=BAR_FILL)
    if fill_w > BAR_R * 2:
        gx, gy = BAR_X + fill_w, BAR_Y + BAR_H // 2
        for i in range(9 * S):
            a = int(70 * (1 - i / (9 * S)))
            bd.ellipse([gx - i, gy - i, gx + i, gy + i], fill=(*CYAN, a))
    cvs = Image.alpha_composite(cvs, blyr)
    draw = ImageDraw.Draw(cvs)

    # ── Watermark ─────────────────────────────────────────────────────────
    wm = "WIND HUNTED UNIVERSE"
    draw.text((CX2 - 16*S - _tw(f_wm, wm), CY2 - 18*S),
              wm, fill=(*DIM, 90), font=f_wm)

    # ── Avatar + black ring ───────────────────────────────────────────────
    if av_img is None:
        av_img = Image.new("RGBA", (AV_SZ, AV_SZ), (45, 45, 65))
    av = av_img.resize((AV_SZ, AV_SZ), Image.LANCZOS)

    RING_PAD = 8 * S          # thickness of the black ring
    ring_sz  = AV_SZ + RING_PAD * 2
    ring     = Image.new("RGBA", (ring_sz, ring_sz), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse([0, 0, ring_sz - 1, ring_sz - 1], fill=(0, 0, 0, 230))
    cvs.paste(ring, (AV_X - RING_PAD, AV_Y - RING_PAD), ring)

    circ = Image.new("RGBA", (AV_SZ, AV_SZ), (0, 0, 0, 0))
    circ.paste(av, (0, 0), _circle_mask(AV_SZ))
    cvs.paste(circ, (AV_X, AV_Y), circ)

    # ── Status dot ────────────────────────────────────────────────────────
    # Sits INSIDE the ring, touching the inner edge at the bottom-right.
    # We offset inward by half the ring thickness so it overlaps the border
    # but stays visually "within" it.

    DOT_R     = 18 * S        # dot radius
    DOT_DIAM  = DOT_R * 2

    # Position: bottom-right of avatar circle, inset from the ring outer edge
    # so the dot centre sits roughly on the avatar circle's edge
    DOT_CX = AV_X + AV_SZ - DOT_R + 4 * S   # slight overlap onto avatar
    DOT_CY = AV_Y + AV_SZ - DOT_R + 4 * S

    # Layer for dot (so we can use alpha_composite for clean transparency)
    dlyr = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
    dd   = ImageDraw.Draw(dlyr)

    # Black outline (matches the ring colour) — bridges dot and ring visually
    OUTLINE = 5 * S
    dd.ellipse([
        DOT_CX - DOT_R - OUTLINE, DOT_CY - DOT_R - OUTLINE,
        DOT_CX + DOT_R + OUTLINE, DOT_CY + DOT_R + OUTLINE,
    ], fill=(0, 0, 0, 235))

    # DND — draw a circle with a horizontal bar cut through it
    if status_name == "dnd":
        dd.ellipse([DOT_CX - DOT_R, DOT_CY - DOT_R,
                    DOT_CX + DOT_R, DOT_CY + DOT_R],
                   fill=(*status_color, 255))
        # White pill bar across the middle
        bar_h = max(3, DOT_R // 2)
        bar_pad = DOT_R // 3
        dd.rounded_rectangle([
            DOT_CX - DOT_R + bar_pad,
            DOT_CY - bar_h // 2,
            DOT_CX + DOT_R - bar_pad,
            DOT_CY + bar_h // 2,
        ], radius=bar_h // 2, fill=(255, 255, 255, 255))

    # IDLE — moon shape: full circle with a smaller circle cut from upper-right
    elif status_name == "idle":
        dd.ellipse([DOT_CX - DOT_R, DOT_CY - DOT_R,
                    DOT_CX + DOT_R, DOT_CY + DOT_R],
                   fill=(*status_color, 255))
        # Cut a circle from top-right to create crescent
        cut_offset = int(DOT_R * 0.45)
        cut_r      = int(DOT_R * 0.75)
        dd.ellipse([
            DOT_CX + cut_offset - cut_r, DOT_CY - cut_offset - cut_r,
            DOT_CX + cut_offset + cut_r, DOT_CY - cut_offset + cut_r,
        ], fill=(0, 0, 0, 235))   # same colour as outline to "erase"

    # INVISIBLE / OFFLINE — hollow circle (outline only)
    elif status_name in ("invisible", "offline"):
        # Outer fill
        dd.ellipse([DOT_CX - DOT_R, DOT_CY - DOT_R,
                    DOT_CX + DOT_R, DOT_CY + DOT_R],
                   fill=(*status_color, 255))
        # Inner hole
        HOLE = max(3, DOT_R // 2)
        dd.ellipse([DOT_CX - HOLE, DOT_CY - HOLE,
                    DOT_CX + HOLE, DOT_CY + HOLE],
                   fill=(0, 0, 0, 235))

    # ONLINE — solid circle
    else:
        dd.ellipse([DOT_CX - DOT_R, DOT_CY - DOT_R,
                    DOT_CX + DOT_R, DOT_CY + DOT_R],
                   fill=(*status_color, 255))

    cvs = Image.alpha_composite(cvs, dlyr)

    # ── Final downscale ───────────────────────────────────────────────────
    out = cvs.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    out.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
