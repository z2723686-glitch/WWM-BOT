# -*- coding: utf-8 -*-
"""
WWM - Database Cleanup System
Automatically purges temporary/log data on a schedule.
NEVER touches: users, guild_settings, role_rewards, welcome_settings, oauth_users

Safe to delete (temporary/log data):
  - reaction_logs     : duplicate-prevention logs (only needed for 24h)
  - admin_logs        : moderation audit trail (keep last 30 days)
  - warnings          : keep, but trim per-user to last 10
  - voice_activity    : session tracking (stale sessions safe to clear)
"""

import logging
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from utils.permissions import is_admin

logger = logging.getLogger("wwm.cleanup")


class DBCleanup(commands.Cog):
    """Auto database cleanup — runs every 24 hours."""

    def __init__(self, bot):
        self.bot = bot
        self.auto_cleanup.start()

    def cog_unload(self):
        self.auto_cleanup.cancel()

    # ── Auto task: runs every 24 hours ───────────────────────

    @tasks.loop(hours=24)
    async def auto_cleanup(self):
        """Silently purge stale data every 24 hours."""
        result = await self._run_cleanup()
        logger.info(
            "Auto cleanup: reaction_logs=%d admin_logs=%d voice=%d",
            result["reaction_logs"],
            result["admin_logs"],
            result["voice_activity"],
        )

    @auto_cleanup.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()
        # Wait 10 minutes after startup before first run
        import asyncio
        await asyncio.sleep(600)

    # ── Core cleanup logic ────────────────────────────────────

    async def _run_cleanup(self) -> dict:
        """
        Purge temporary data. Returns dict of deleted counts.
        SAFE COLLECTIONS ONLY — users/guild_settings/role_rewards untouched.
        """
        now = datetime.now(timezone.utc)
        counts = {
            "reaction_logs": 0,
            "admin_logs":    0,
            "voice_activity": 0,
            "warnings_trimmed": 0,
        }

        db = self.bot.db

        # 1. reaction_logs — delete everything older than 48 hours
        #    (only needed for 24h dedup, 48h is safe buffer)
        cutoff_48h = now - timedelta(hours=48)
        r = await db.reaction_logs.delete_many(
            {"timestamp": {"$lt": cutoff_48h}}
        )
        counts["reaction_logs"] = r.deleted_count

        # 2. admin_logs — keep only last 30 days
        cutoff_30d = now - timedelta(days=30)
        r = await db.admin_logs.delete_many(
            {"timestamp": {"$lt": cutoff_30d}}
        )
        counts["admin_logs"] = r.deleted_count

        # 3. voice_activity — clear stale sessions older than 12 hours
        #    (a session open for 12h+ is definitely a crash/disconnect remnant)
        cutoff_12h = now - timedelta(hours=12)
        r = await db.voice_activity.delete_many(
            {"last_join": {"$lt": cutoff_12h}}
        )
        counts["voice_activity"] = r.deleted_count

        # 4. warnings — per user, keep only the 10 most recent
        #    Finds users with >10 warnings and trims the oldest ones
        pipeline = [
            {"$group": {"_id": {"guild_id": "$guild_id", "user_id": "$user_id"},
                        "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 10}}},
        ]
        async for doc in db.warnings.aggregate(pipeline):
            gid = doc["_id"]["guild_id"]
            uid = doc["_id"]["user_id"]
            # Get oldest warning IDs beyond the 10 most recent
            cursor = db.warnings.find(
                {"guild_id": gid, "user_id": uid},
                {"_id": 1}
            ).sort("timestamp", -1).skip(10)
            old_ids = [d["_id"] async for d in cursor]
            if old_ids:
                r = await db.warnings.delete_many({"_id": {"$in": old_ids}})
                counts["warnings_trimmed"] += r.deleted_count

        return counts

    # ── /dbstats — see what's using space ────────────────────

    @app_commands.command(
        name="dbstats",
        description="Show database collection sizes and document counts"
    )
    @is_admin()
    async def dbstats(self, interaction: discord.Interaction):
        """Show how much space each collection is using."""
        await interaction.response.defer(ephemeral=True)

        db     = self.bot.db
        lines  = []
        total  = 0

        collections = [
            ("users",          "👤 Users (XP/levels)"),
            ("guild_settings", "⚙️  Guild settings"),
            ("role_rewards",   "🎖️  Role rewards"),
            ("warnings",       "⚠️  Warnings"),
            ("reaction_logs",  "⭐ Reaction logs"),
            ("admin_logs",     "📋 Admin logs"),
            ("voice_activity", "🎙️  Voice activity"),
            ("oauth_users",    "🔐 OAuth users"),
        ]

        for coll_name, label in collections:
            try:
                stats = await db.command("collStats", coll_name)
                size  = stats.get("storageSize", 0)
                count = stats.get("count", 0)
                kb    = size / 1024
                total += size
                lines.append(
                    "{} `{:>5.0f} KB`  {:,} docs".format(label, kb, count)
                )
            except Exception:
                count = await db[coll_name].count_documents({})
                lines.append("{} `  ??? KB`  {:,} docs".format(label, count))

        total_mb = total / (1024 * 1024)
        limit_mb = 512
        pct      = (total_mb / limit_mb) * 100
        bar_len  = 20
        filled   = int(bar_len * pct / 100)
        bar      = "\u2588" * filled + "\u2591" * (bar_len - filled)

        embed = discord.Embed(
            title="Database Storage",
            color=Config.COLOR_INFO,
        )
        embed.add_field(
            name="Collections",
            value="\n".join(lines),
            inline=False,
        )
        embed.add_field(
            name="Total Used",
            value="`{}` {:.1f} MB / {} MB ({:.1f}%)".format(
                bar, total_mb, limit_mb, pct
            ),
            inline=False,
        )
        embed.set_footer(text="Use /dbcleanup to free space")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /dbcleanup — manual trigger ──────────────────────────

    @app_commands.command(
        name="dbcleanup",
        description="Manually run database cleanup to free space"
    )
    @is_admin()
    async def dbcleanup(self, interaction: discord.Interaction):
        """Manually trigger cleanup. Safe — never deletes user XP/levels."""
        await interaction.response.defer(ephemeral=True)

        counts = await self._run_cleanup()

        total_deleted = (
            counts["reaction_logs"] +
            counts["admin_logs"] +
            counts["voice_activity"] +
            counts["warnings_trimmed"]
        )

        embed = discord.Embed(
            title="Cleanup Complete",
            color=Config.COLOR_SUCCESS,
        )
        embed.add_field(
            name="Documents Removed",
            value=(
                "Reaction logs:  `{}`\n"
                "Admin logs:     `{}`\n"
                "Voice activity: `{}`\n"
                "Old warnings:   `{}`\n"
                "\n**Total: `{}` documents**"
            ).format(
                counts["reaction_logs"],
                counts["admin_logs"],
                counts["voice_activity"],
                counts["warnings_trimmed"],
                total_deleted,
            ),
            inline=False,
        )
        embed.add_field(
            name="Safe — Not Touched",
            value=(
                "Users, XP, levels, messages, voice minutes\n"
                "Guild settings, role rewards, welcome settings"
            ),
            inline=False,
        )
        embed.set_footer(text="Auto-cleanup runs every 24 hours")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(DBCleanup(bot))
