# -*- coding: utf-8 -*-
"""
WWM - XP System
Levels now start at 1. A brand-new user begins at Level 1, not 0.
"""

import random


# -- Formula constants --
XP_BASE     = 100
XP_EXPONENT = 1.5
XP_MIN      = 10
XP_MAX      = 25


def xp_for_level(level: int) -> int:
    """XP required to advance FROM level N to level N+1."""
    if level < 1:
        return 0
    return int(XP_BASE * (level ** XP_EXPONENT))


def calculate_level_from_xp(total_xp: int):
    """
    Given total XP, return (level, xp_into_current_level, xp_needed_for_next).
    Level starts at 1. A user with 0 XP is Level 1 with 0/100 progress.
    """
    level = 1
    remaining = total_xp

    while True:
        needed = xp_for_level(level)
        if remaining < needed:
            break
        remaining -= needed
        level += 1

    return level, remaining, xp_for_level(level)


def get_random_xp(base: int = None) -> int:
    if base:
        variance = int(base * 0.3)
        return max(1, base + random.randint(-variance, variance))
    return random.randint(XP_MIN, XP_MAX)


def calculate_progress_percentage(current_xp: int, needed_xp: int) -> float:
    if needed_xp <= 0:
        return 1.0
    return min(1.0, current_xp / needed_xp)


def format_xp(xp: int) -> str:
    if xp >= 1_000_000:
        return "{:.1f}M".format(xp / 1_000_000)
    if xp >= 1_000:
        return "{:.1f}K".format(xp / 1_000)
    return str(xp)


def format_voice_time(minutes: int) -> str:
    if minutes >= 60:
        return "{}h {}m".format(minutes // 60, minutes % 60)
    return "{}m".format(minutes)


def get_level_title(level: int) -> str:
    if level >= 50:
        return "Mythic"
    if level >= 30:
        return "Legend"
    if level >= 20:
        return "Veteran"
    if level >= 10:
        return "Elite"
    if level >= 5:
        return "Hunter"
    return "Wanderer"
