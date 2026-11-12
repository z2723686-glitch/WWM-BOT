# -*- coding: utf-8 -*-
"""
WWM - Status & Limits Monitor
Shows real-time health of:
  - Bot itself (latency, uptime, guilds, memory)
  - MongoDB (storage used vs 512 MB free limit, connections, collections)
  - Railway free tier credit awareness
  - Per-collection document counts
  - Automatic warning DM to bot owner when DB > 80%

Commands:
  /status   - full health dashboard (admin only)
  /botstats - quick public bot stats
"""

import logging
import os
import platform
import sys
import time
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from utils.permissions import is_admin

logger = logging.getLogger("wwm.status")

# MongoDB free tier hard limits
MONGO_STORAGE_LIMIT_MB   = 512
MONGO_CONN_LIMIT         = 500
MONGO_OPS_PER_SEC_LIMIT  = 100

# Warning thresholds
WARN_STORAGE_PCT  = 80   # warn owner when DB > 80%
CRIT_STORAGE_PCT  = 95   # critical when DB > 95%


def _fmt_bytes(b: int) -> str:
    if b >= 1024 * 1024:
        return "{:.2f} MB".format(b / (1024 * 1024))
    if b >= 1024:
        return "{:.1f} KB".format(b / 1024)
    return "{} B".format(b)


def _progress_bar(pct: float, length: int = 16) -> str:
    filled = int(length * pct / 100)
    filled = max(0, min(length, filled))
    bar    = "\u2588" * filled + "\u2591" * (length - filled)
    if pct >= CRIT_STORAGE_PCT:
        return bar + " \U0001f534"   # red circle
    if pct >= WARN_STORAGE_PCT:
        return bar + " \U0001f7e1"   # yellow circle
    return bar + " \U0001f7e2"       # green circle


def _uptime_str(start_time: datetime) -> str:
    delta = datetime.now(timezone.utc) - start_time
    d = delta.days
    h, rem = divmod(delta.seconds, 3600)
    m, s   = divmod(rem, 60)
    parts = []
    if d: parts.append("{}d".format(d))
    if h: parts.append("{}h".format(h))
    if m: parts.append("{}m".format(m))
    parts.append("{}s".format(s))
    return " ".join(parts)


async def _get_db_stats(bot) -> dict:
    """
    Fetch MongoDB storage stats.
    Returns dict with storage_mb, pct, collections breakdown, total_docs.
    Falls back gracefully if dbStats is not permitted.
    """
    stats = {
        "storage_mb":   0.0,
        "data_mb":      0.0,
        "pct":          0.0,
        "collections":  {},
        "total_docs":   0,
        "error":        None,
    }

    COLLECTIONS = [
        "users",
        "guild_settings",
        "role_rewards",
        "warnings",
        "reaction_logs",
        "admin_logs",
        "voice_activity",
        "oauth_users",
    ]

    total_storage = 0
    total_data    = 0
    total_docs    = 0

    for name in COLLECTIONS:
        try:
            result = await bot.db.db.command("collStats", name)
            storage = result.get("storageSize", 0)
            data    = result.get("size", 0)
            count   = result.get("count", 0)
            total_storage += storage
            total_data    += data
            total_docs    += count
            stats["collections"][name] = {
                "storage_bytes": storage,
                "data_bytes":    data,
                "docs":          count,
            }
        except Exception:
            # collStats not available — fall back to count only
            try:
                count = await bot.db.db[name].count_documents({})
                total_docs += count
                stats["collections"][name] = {
                    "storage_bytes": 0,
                    "data_bytes":    0,
                    "docs":          count,
                }
            except Exception as e:
                stats["collections"][name] = {"storage_bytes": 0, "data_bytes": 0, "docs": 0}
                stats["error"] = str(e)

    # Try dbStats for total (more accurate than summing collStats)
    try:
        db_result    = await bot.db.db.command("dbStats")
        total_storage = db_result.get("storageSize", total_storage)
        total_data    = db_result.get("dataSize",    total_data)
    except Exception:
        pass

    stats["storage_mb"] = total_storage / (1024 * 1024)
    stats["data_mb"]    = total_data    / (1024 * 1024)
    stats["pct"]        = (stats["storage_mb"] / MONGO_STORAGE_LIMIT_MB) * 100
    stats["total_docs"] = total_docs
    return stats


async def _get_memory_mb() -> float:
    """Get current process memory usage in MB."""
    try:
        import resource
        mem_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux returns KB, macOS returns bytes
        if platform.system() == "Linux":
            return mem_bytes / 1024
        return mem_bytes / (1024 * 1024)
    except Exception:
        pass
    try:
        import subprocess
        pid    = os.getpid()
        result = subprocess.check_output(
            ["cat", "/proc/{}/status".format(pid)], text=True
        )
        for line in result.splitlines():
            if "VmRSS:" in line:
                return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0


class Status(commands.Cog):
    """Status monitoring cog with auto alerts."""

    def __init__(self, bot):
        self.bot               = bot
        self._warned_80        = False   # track if we already sent 80% warning
        self._warned_95        = False
        self.monitor_loop.start()

    def cog_unload(self):
        self.monitor_loop.cancel()

    # ── Background monitor (every 6 hours) ───────────────────

    @tasks.loop(hours=6)
    async def monitor_loop(self):
        """Check DB usage every 6 hours and DM bot owner if critical."""
        try:
            db_stats = await _get_db_stats(self.bot)
            pct      = db_stats["pct"]

            owner_id = os.getenv("OWNER_ID", "")
            if not owner_id:
                return

            owner = await self.bot.fetch_user(int(owner_id))
            if not owner:
                return

            if pct >= CRIT_STORAGE_PCT and not self._warned_95:
                self._warned_95 = True
                embed = discord.Embed(
                    title="\U0001f534 CRITICAL: Database Almost Full",
                    description=(
                        "MongoDB storage is at **{:.1f}%** "
                        "({:.2f} MB / {} MB).\n\n"
                        "**Immediate action needed!**\n"
                        "Run `/dbcleanup` now or upgrade your plan."
                    ).format(pct, db_stats["storage_mb"], MONGO_STORAGE_LIMIT_MB),
                    color=Config.COLOR_ERROR,
                    timestamp=datetime.now(timezone.utc),
                )
                await owner.send(embed=embed)
                logger.critical("DB at %.1f%% — DM sent to owner", pct)

            elif pct >= WARN_STORAGE_PCT and not self._warned_80:
                self._warned_80 = True
                embed = discord.Embed(
                    title="\U0001f7e1 Warning: Database Filling Up",
                    description=(
                        "MongoDB storage is at **{:.1f}%** "
                        "({:.2f} MB / {} MB).\n\n"
                        "Consider running `/dbcleanup` soon."
                    ).format(pct, db_stats["storage_mb"], MONGO_STORAGE_LIMIT_MB),
                    color=Config.COLOR_WARNING,
                    timestamp=datetime.now(timezone.utc),
                )
                await owner.send(embed=embed)
                logger.warning("DB at %.1f%% — warning DM sent to owner", pct)

            # Reset flags if usage dropped (after cleanup)
            if pct < WARN_STORAGE_PCT:
                self._warned_80 = False
                self._warned_95 = False

        except Exception as e:
            logger.error("Monitor loop error: %s", e)

    @monitor_loop.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()

    # ── /status — full admin dashboard ───────────────────────

    @app_commands.command(
        name="status",
        description="Full bot and database health dashboard"
    )
    @is_admin()
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # --- Bot stats ---
        latency_ms  = round(self.bot.latency * 1000)
        uptime      = _uptime_str(self.bot.start_time)
        guild_count = len(self.bot.guilds)
        user_count  = sum(g.member_count for g in self.bot.guilds)
        mem_mb      = await _get_memory_mb()
        py_ver      = "{}.{}.{}".format(*sys.version_info[:3])

        # Latency color
        if latency_ms < 100:
            lat_icon = "\U0001f7e2"   # green
        elif latency_ms < 200:
            lat_icon = "\U0001f7e1"   # yellow
        else:
            lat_icon = "\U0001f534"   # red

        # --- DB stats ---
        db_stats = await _get_db_stats(self.bot)
        pct      = db_stats["pct"]
        db_bar   = _progress_bar(pct)

        # --- Build embed ---
        embed = discord.Embed(
            title="\U0001f4ca Where Winds Meet — Status",
            color=(
                Config.COLOR_ERROR   if pct >= CRIT_STORAGE_PCT else
                Config.COLOR_WARNING if pct >= WARN_STORAGE_PCT  else
                Config.COLOR_SUCCESS
            ),
            timestamp=datetime.now(timezone.utc),
        )

        # Bot section
        embed.add_field(
            name="\U0001f916 Bot",
            value=(
                "{} Latency: **{}ms**\n"
                "\u23f1\ufe0f Uptime: **{}**\n"
                "\U0001f30d Guilds: **{:,}**\n"
                "\U0001f465 Users: **{:,}**\n"
                "\U0001f9e0 Memory: **{:.1f} MB**\n"
                "\U0001f40d Python: **{}**"
            ).format(
                lat_icon, latency_ms,
                uptime,
                guild_count,
                user_count,
                mem_mb,
                py_ver,
            ),
            inline=True,
        )

        # Railway section
        embed.add_field(
            name="\U0001f686 Railway Free Tier",
            value=(
                "\U0001f4be RAM limit: **512 MB**\n"
                "\U0001f9e0 RAM used: **{:.1f} MB**\n"
                "\U0001f4bb vCPU: **1 (shared)**\n"
                "\U0001f4b3 Plan: **Starter ($5 trial)**\n"
                "\u26a0\ufe0f Credits end = **bot stops**"
            ).format(mem_mb),
            inline=True,
        )

        embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer

        # MongoDB section
        embed.add_field(
            name="\U0001f4c0 MongoDB Atlas (M0 Free)",
            value=(
                "`{}` **{:.1f}%**\n"
                "Used:  **{:.2f} MB** / {} MB\n"
                "Free:  **{:.2f} MB** remaining\n"
                "Docs:  **{:,}** total\n"
                "Limit: **500 connections | 100 ops/s**"
            ).format(
                db_bar, pct,
                db_stats["storage_mb"], MONGO_STORAGE_LIMIT_MB,
                max(0, MONGO_STORAGE_LIMIT_MB - db_stats["storage_mb"]),
                db_stats["total_docs"],
            ),
            inline=False,
        )

        # Per-collection breakdown
        COLL_LABELS = {
            "users":          "\U0001f464 Users",
            "guild_settings": "\u2699\ufe0f  Settings",
            "role_rewards":   "\U0001f3c5 Roles",
            "warnings":       "\u26a0\ufe0f  Warnings",
            "reaction_logs":  "\u2b50 Reactions",
            "admin_logs":     "\U0001f4cb Mod logs",
            "voice_activity": "\U0001f399\ufe0f  Voice",
            "oauth_users":    "\U0001f510 OAuth",
        }
        SAFE_COLLS = {"users", "guild_settings", "role_rewards"}

        lines = []
        for coll, label in COLL_LABELS.items():
            info    = db_stats["collections"].get(coll, {})
            docs    = info.get("docs", 0)
            storage = info.get("storage_bytes", 0)
            size_str = _fmt_bytes(storage) if storage else "—"
            safe_tag = " \U0001f512" if coll in SAFE_COLLS else ""
            lines.append("`{:<16}` {:>6,} docs  {:>10}{}".format(
                label, docs, size_str, safe_tag
            ))

        embed.add_field(
            name="\U0001f5c2\ufe0f Collections  (\U0001f512 = never auto-deleted)",
            value="```\n{}\n```".format("\n".join(lines)),
            inline=False,
        )

        # Health verdict
        if pct >= CRIT_STORAGE_PCT:
            verdict = "\U0001f534 **CRITICAL** — Run `/dbcleanup` immediately!"
        elif pct >= WARN_STORAGE_PCT:
            verdict = "\U0001f7e1 **WARNING** — Storage filling up. Run `/dbcleanup` soon."
        elif latency_ms > 300:
            verdict = "\U0001f7e1 **WARNING** — High latency detected."
        else:
            verdict = "\U0001f7e2 **Healthy** — All systems normal."

        embed.add_field(name="Health", value=verdict, inline=False)

        if db_stats.get("error"):
            embed.set_footer(
                text="Note: Detailed storage stats unavailable on M0 free tier — showing estimates"
            )
        else:
            embed.set_footer(text="Where Winds Meet • Auto-monitor runs every 6h")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /botstats — public stats (beautified) ────────────────

    @app_commands.command(
        name="botstats",
        description="View bot statistics"
    )
    async def botstats(self, interaction: discord.Interaction):
        await interaction.response.defer()

        latency_ms  = round(self.bot.latency * 1000)
        uptime      = _uptime_str(self.bot.start_time)
        guild_count = len(self.bot.guilds)
        user_count  = sum(g.member_count for g in self.bot.guilds)

        if latency_ms < 100:
            lat_icon  = "\U0001f7e2"
            lat_label = "Excellent"
        elif latency_ms < 200:
            lat_icon  = "\U0001f7e1"
            lat_label = "Good"
        elif latency_ms < 400:
            lat_icon  = "\U0001f7e0"
            lat_label = "Moderate"
        else:
            lat_icon  = "\U0001f534"
            lat_label = "High"

        embed = discord.Embed(
            description=(
                "### \U0001f30c Where Winds Meet\n"
                "Community bot for the Jianghu."
            ),
            color=Config.COLOR_PRIMARY,
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(
            name="\U0001f4e1  Latency",
            value="{} **{}ms** \u2014 `{}`".format(lat_icon, latency_ms, lat_label),
            inline=False,
        )
        embed.add_field(
            name="\u23f1\ufe0f  Uptime",
            value="**{}**".format(uptime),
            inline=True,
        )
        embed.add_field(
            name="\U0001f30d  Servers",
            value="**{:,}**".format(guild_count),
            inline=True,
        )
        embed.add_field(
            name="\U0001f465  Members",
            value="**{:,}**".format(user_count),
            inline=True,
        )

        # Live DB activity
        try:
            ranked = await self.bot.db.users.count_documents({"xp": {"$gt": 0}})
            agg    = await self.bot.db.users.aggregate([
                {"$group": {"_id": None, "t": {"$sum": "$messages"}}}
            ]).to_list(1)
            msgs = agg[0]["t"] if agg else 0
            embed.add_field(
                name="\u26a1  Ranked Members",
                value="**{:,}**".format(ranked),
                inline=True,
            )
            embed.add_field(
                name="\U0001f4ac  Messages Tracked",
                value="**{:,}**".format(msgs),
                inline=True,
            )
        except Exception:
            pass

        embed.set_footer(text="Where Winds Meet \u2022 /rank to see your level")
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Status(bot))
