# -*- coding: utf-8 -*-
"""
WWM - Permissions
Bot owner (OWNER_ID env var) bypasses ALL permission checks automatically.
"""

import os
import logging
import discord
from discord import app_commands

logger = logging.getLogger("wwm.permissions")


def _is_owner(user_id: int) -> bool:
    """
    Return True if the user is the bot owner or a trusted admin.
    OWNER_ID     = single owner (env var)
    ADMIN_IDS    = comma-separated extra admins (env var)
    Example:  ADMIN_IDS=123456789,987654321
    """
    # Check main owner
    owner_id = os.getenv("OWNER_ID", "")
    try:
        if owner_id and user_id == int(owner_id):
            return True
    except ValueError:
        pass

    # Check extra admins
    admin_ids = os.getenv("ADMIN_IDS", "")
    for id_str in admin_ids.split(","):
        id_str = id_str.strip()
        try:
            if id_str and user_id == int(id_str):
                return True
        except ValueError:
            pass

    return False


def is_admin():
    """
    Administrator permission check.
    Bot owner always passes regardless of guild permissions.
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        # Owner bypass — always allowed
        if _is_owner(interaction.user.id):
            return True
        # Normal admin check
        if interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message(
            embed=discord.Embed(
                title="\u274c Access Denied",
                description="You need **Administrator** permission to use this command.",
                color=0xE74C3C,
            ),
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)


def is_moderator():
    """
    Moderator permission check.
    Bot owner always passes.
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        if _is_owner(interaction.user.id):
            return True
        perms = interaction.user.guild_permissions
        if perms.administrator or perms.manage_messages or perms.kick_members:
            return True
        await interaction.response.send_message(
            embed=discord.Embed(
                title="\u274c Access Denied",
                description="You need **Moderator** permissions to use this command.",
                color=0xE74C3C,
            ),
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)


def bot_has_permissions(**perms):
    """Check that the bot itself has required permissions in the guild."""
    async def predicate(interaction: discord.Interaction) -> bool:
        bot_perms = interaction.guild.me.guild_permissions
        missing   = [p for p, v in perms.items()
                     if not getattr(bot_perms, p, False) and v]
        if missing:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="\u274c Missing Bot Permissions",
                    description="I need: " + ", ".join("`{}`".format(p) for p in missing),
                    color=0xE74C3C,
                ),
                ephemeral=True,
            )
            return False
        return True
    return app_commands.check(predicate)


def can_moderate_target(interaction: discord.Interaction, target: discord.Member) -> bool:
    """Check role hierarchy before a moderation action."""
    if target == interaction.guild.me:
        return False
    if target == interaction.guild.owner:
        return False
    if interaction.user.top_role <= target.top_role:
        # Owner can bypass role hierarchy check
        if _is_owner(interaction.user.id):
            return True
        return False
    if interaction.guild.me.top_role <= target.top_role:
        return False
    return True


def create_error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title="\u274c " + title, description=description, color=0xE74C3C)


def create_success_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title="\u2705 " + title, description=description, color=0x2ECC71)


# ── Channel restriction ───────────────────────────────────────────────────────

# Commands locked to this channel (rank, xp, leaderboard, top)
LEVEL_CHANNEL_ID = 1513514239502057594


def in_level_channel():
    """
    Restricts /rank /xp /leaderboard /top to the designated level channel.
    Bot owner bypasses this check too.
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        # Owner always allowed anywhere
        if _is_owner(interaction.user.id):
            return True
        # Correct channel — allow
        if interaction.channel_id == LEVEL_CHANNEL_ID:
            return True
        # Wrong channel — redirect
        await interaction.response.send_message(
            embed=discord.Embed(
                description=(
                    "\U0001f4ca This command only works in <#{}>.\n"
                    "Head over there to check your level!"
                ).format(LEVEL_CHANNEL_ID),
                color=0xF39C12,
            ),
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)
