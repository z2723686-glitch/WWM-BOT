"""
WHERE WINDS MEET - Moderation Cog
=====================================
Full moderation suite: ban, kick, timeout, warn, clear, mute, unmute.
All actions logged to mod-log channel with premium embeds.
"""

import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from utils.permissions import is_moderator, can_moderate_target

logger = logging.getLogger("wwm.moderation")


def mod_embed(action: str, target: discord.Member, moderator: discord.Member,
              reason: str, color: int, extra_fields: list = None) -> discord.Embed:
    """Create a standardized moderation embed."""
    action_emojis = {
        "Ban": "🔨", "Kick": "👢", "Timeout": "⏰",
        "Warn": "⚠️", "Mute": "🔇", "Unmute": "🔊",
        "Clear": "🧹"
    }
    emoji = action_emojis.get(action, "⚡")

    embed = discord.Embed(
        title=f"{emoji} {action}",
        color=color,
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="👤 Target", value=f"{target.mention} (`{target.id}`)", inline=True)
    embed.add_field(name="🛡️ Moderator", value=f"{moderator.mention}", inline=True)
    embed.add_field(name="📋 Reason", value=reason or "No reason provided", inline=False)

    if extra_fields:
        for name, value, inline in extra_fields:
            embed.add_field(name=name, value=value, inline=inline)

    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text="Where Winds Meet • Moderation")
    return embed


class Moderation(commands.Cog):
    """Full moderation suite for the WWM Bot."""

    def __init__(self, bot):
        self.bot = bot

    async def _send_to_log(self, guild: discord.Guild, embed: discord.Embed):
        """Send a moderation log to the configured log channel."""
        try:
            settings = await self.bot.db.get_guild_settings(str(guild.id))
            log_channel_id = settings.get("log_channel")
            if log_channel_id:
                channel = guild.get_channel(int(log_channel_id))
                if channel:
                    await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send mod log: {e}")

    async def _check_target(
        self,
        interaction: discord.Interaction,
        target: discord.Member
    ) -> bool:
        """Validate that a moderation target is valid."""
        if not can_moderate_target(interaction, target):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Cannot Moderate Target",
                    description=(
                        "You cannot moderate this user. Possible reasons:\n"
                        "• They have equal or higher role than you\n"
                        "• They are the server owner\n"
                        "• The bot lacks sufficient permissions"
                    ),
                    color=Config.COLOR_ERROR
                ),
                ephemeral=True
            )
            return False
        return True

    # ─────────────────────────────────────────
    # BAN
    # ─────────────────────────────────────────

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(
        member="The member to ban",
        reason="Reason for the ban",
        delete_days="Delete message history (0-7 days)"
    )
    @is_moderator()
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
        delete_days: app_commands.Range[int, 0, 7] = 0
    ):
        if not await self._check_target(interaction, member):
            return

        try:
            # Attempt to DM user before ban
            try:
                await member.send(
                    embed=discord.Embed(
                        title=f"🔨 Banned from {interaction.guild.name}",
                        description=f"**Reason:** {reason}",
                        color=Config.COLOR_ERROR
                    )
                )
            except discord.HTTPException:
                pass

            await member.ban(reason=f"{reason} | By: {interaction.user}", delete_message_days=delete_days)

            embed = mod_embed("Ban", member, interaction.user, reason, Config.COLOR_ERROR)

            await interaction.response.send_message(embed=embed)
            await self._send_to_log(interaction.guild, embed)
            await self.bot.db.log_admin_action(
                str(interaction.guild.id), str(interaction.user.id),
                "ban", str(member.id), reason
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Missing Permissions", description="I cannot ban this member.", color=Config.COLOR_ERROR),
                ephemeral=True
            )

    # ─────────────────────────────────────────
    # KICK
    # ─────────────────────────────────────────

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="The member to kick", reason="Reason for the kick")
    @is_moderator()
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided"
    ):
        if not await self._check_target(interaction, member):
            return

        try:
            try:
                await member.send(
                    embed=discord.Embed(
                        title=f"👢 Kicked from {interaction.guild.name}",
                        description=f"**Reason:** {reason}",
                        color=Config.COLOR_WARNING
                    )
                )
            except discord.HTTPException:
                pass

            await member.kick(reason=f"{reason} | By: {interaction.user}")

            embed = mod_embed("Kick", member, interaction.user, reason, Config.COLOR_WARNING)
            await interaction.response.send_message(embed=embed)
            await self._send_to_log(interaction.guild, embed)
            await self.bot.db.log_admin_action(
                str(interaction.guild.id), str(interaction.user.id),
                "kick", str(member.id), reason
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Missing Permissions", color=Config.COLOR_ERROR),
                ephemeral=True
            )

    # ─────────────────────────────────────────
    # TIMEOUT
    # ─────────────────────────────────────────

    @app_commands.command(name="timeout", description="Timeout a member (temporary mute)")
    @app_commands.describe(
        member="The member to timeout",
        minutes="Duration in minutes (1-43200)",
        reason="Reason for the timeout"
    )
    @is_moderator()
    @app_commands.checks.has_permissions(moderate_members=True)
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, 43200] = 10,
        reason: str = "No reason provided"
    ):
        if not await self._check_target(interaction, member):
            return

        duration = timedelta(minutes=minutes)
        until = datetime.utcnow() + duration

        try:
            await member.timeout(duration, reason=f"{reason} | By: {interaction.user}")

            embed = mod_embed(
                "Timeout", member, interaction.user, reason, Config.COLOR_WARNING,
                extra_fields=[("⏱️ Duration", f"{minutes} minutes", True),
                               ("🕐 Expires", f"<t:{int(until.timestamp())}:R>", True)]
            )
            await interaction.response.send_message(embed=embed)
            await self._send_to_log(interaction.guild, embed)
            await self.bot.db.log_admin_action(
                str(interaction.guild.id), str(interaction.user.id),
                f"timeout_{minutes}m", str(member.id), reason
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Missing Permissions", color=Config.COLOR_ERROR),
                ephemeral=True
            )

    # ─────────────────────────────────────────
    # WARN
    # ─────────────────────────────────────────

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.describe(member="The member to warn", reason="Reason for the warning")
    @is_moderator()
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided"
    ):
        if not await self._check_target(interaction, member):
            return

        warning_count = await self.bot.db.add_warning(
            str(interaction.guild.id),
            str(member.id),
            str(interaction.user.id),
            reason
        )

        try:
            await member.send(
                embed=discord.Embed(
                    title=f"⚠️ Warning from {interaction.guild.name}",
                    description=f"**Reason:** {reason}\n**Total Warnings:** {warning_count}",
                    color=Config.COLOR_WARNING
                )
            )
        except discord.HTTPException:
            pass

        embed = mod_embed(
            "Warn", member, interaction.user, reason, Config.COLOR_WARNING,
            extra_fields=[("📊 Total Warnings", str(warning_count), True)]
        )
        await interaction.response.send_message(embed=embed)
        await self._send_to_log(interaction.guild, embed)

        # Auto-ban at max warnings
        if warning_count >= Config.MAX_WARNINGS_BEFORE_BAN:
            try:
                await member.ban(reason=f"Auto-ban: {warning_count} warnings reached")
                ban_embed = discord.Embed(
                    title="🔨 Auto-Ban Triggered",
                    description=f"{member.mention} was auto-banned after {warning_count} warnings.",
                    color=Config.COLOR_ERROR
                )
                await self._send_to_log(interaction.guild, ban_embed)
            except Exception:
                pass

    # ─────────────────────────────────────────
    # CLEAR
    # ─────────────────────────────────────────

    @app_commands.command(name="clear", description="Delete messages from a channel")
    @app_commands.describe(
        amount="Number of messages to delete (1-100)",
        member="Only delete messages from this member"
    )
    @is_moderator()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def clear(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100] = 10,
        member: discord.Member = None
    ):
        await interaction.response.defer(ephemeral=True)

        def check(msg):
            if member:
                return msg.author == member
            return True

        try:
            deleted = await interaction.channel.purge(limit=amount, check=check)

            embed = discord.Embed(
                title="🧹 Messages Cleared",
                description=f"Deleted **{len(deleted)}** messages" +
                            (f" from {member.mention}" if member else ""),
                color=Config.COLOR_SUCCESS
            )
            embed.set_footer(text=f"By: {interaction.user}")
            await interaction.followup.send(embed=embed, ephemeral=True)
            await self._send_to_log(interaction.guild, embed)

        except discord.Forbidden:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Missing Permissions", color=Config.COLOR_ERROR),
                ephemeral=True
            )

    # ─────────────────────────────────────────
    # WARNINGS CHECK
    # ─────────────────────────────────────────

    @app_commands.command(name="warnings", description="View warnings for a member")
    @app_commands.describe(member="The member to check warnings for")
    @is_moderator()
    async def warnings(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ):
        warns = await self.bot.db.get_warnings(str(interaction.guild.id), str(member.id))

        embed = discord.Embed(
            title=f"⚠️ Warnings — {member.display_name}",
            color=Config.COLOR_WARNING
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        if not warns:
            embed.description = "✅ This member has no warnings."
        else:
            embed.description = f"**Total:** {len(warns)} warning(s)\n"
            for i, w in enumerate(warns[:10], 1):
                ts = w.get("timestamp", datetime.utcnow())
                embed.add_field(
                    name=f"Warning #{i}",
                    value=f"**Reason:** {w['reason']}\n**Date:** <t:{int(ts.timestamp())}:d>",
                    inline=False
                )

        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────
    # UNBAN
    # ─────────────────────────────────────────

    @app_commands.command(name="unban", description="Unban a user by ID")
    @app_commands.describe(user_id="The user ID to unban", reason="Reason for unban")
    @is_moderator()
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: str = "No reason provided"
    ):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=reason)

            embed = discord.Embed(
                title="✅ User Unbanned",
                description=f"**{user}** (`{user.id}`) has been unbanned.\n**Reason:** {reason}",
                color=Config.COLOR_SUCCESS
            )
            await interaction.response.send_message(embed=embed)
            await self._send_to_log(interaction.guild, embed)

        except discord.NotFound:
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ User not found or not banned", color=Config.COLOR_ERROR),
                ephemeral=True
            )
        except ValueError:
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Invalid user ID", color=Config.COLOR_ERROR),
                ephemeral=True
            )

    # ─────────────────────────────────────────
    # UNTIMEOUT / UNMUTE
    # ─────────────────────────────────────────

    @app_commands.command(name="untimeout", description="Remove timeout from a member")
    @app_commands.describe(member="The member to remove timeout from")
    @is_moderator()
    @app_commands.checks.has_permissions(moderate_members=True)
    async def untimeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ):
        try:
            await member.timeout(None)
            embed = discord.Embed(
                title="🔊 Timeout Removed",
                description=f"{member.mention}'s timeout has been removed by {interaction.user.mention}.",
                color=Config.COLOR_SUCCESS
            )
            await interaction.response.send_message(embed=embed)
            await self._send_to_log(interaction.guild, embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Missing Permissions", color=Config.COLOR_ERROR),
                ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(Moderation(bot))
