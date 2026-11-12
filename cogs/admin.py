"""
WHERE WINDS MEET - Admin Cog
================================
Server configuration slash commands for administrators.
All settings stored per-guild in MongoDB.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from utils.permissions import is_admin

logger = logging.getLogger("wwm.admin")


class Admin(commands.Cog):
    """Admin configuration commands for WWM Bot."""

    def __init__(self, bot):
        self.bot = bot

    # ─────────────────────────────────────────
    # LEVEL-UP CHANNEL
    # ─────────────────────────────────────────

    @app_commands.command(name="setlevelchannel", description="Set the channel for level-up announcements")
    @app_commands.describe(channel="The channel to send level-up messages")
    @is_admin()
    async def set_level_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel
    ):
        await self.bot.db.update_guild_settings(
            str(interaction.guild.id),
            {"level_up_channel": str(channel.id)}
        )
        embed = discord.Embed(
            title="✅ Level-Up Channel Set",
            description=f"Level-up announcements will be sent to {channel.mention}",
            color=Config.COLOR_SUCCESS
        )
        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────
    # XP SETTINGS
    # ─────────────────────────────────────────

    @app_commands.command(name="setxp", description="Configure XP per message and cooldown")
    @app_commands.describe(
        xp_amount="XP awarded per message (5-100)",
        cooldown="Seconds between XP awards (10-300)"
    )
    @is_admin()
    async def set_xp(
        self,
        interaction: discord.Interaction,
        xp_amount: app_commands.Range[int, 5, 100] = None,
        cooldown: app_commands.Range[int, 10, 300] = None
    ):
        update = {}
        desc_parts = []

        if xp_amount is not None:
            update["xp_per_message"] = xp_amount
            desc_parts.append(f"XP per message: **{xp_amount}**")

        if cooldown is not None:
            update["xp_cooldown"] = cooldown
            desc_parts.append(f"XP cooldown: **{cooldown}s**")

        if not update:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ No Changes",
                    description="Provide at least one parameter to update.",
                    color=Config.COLOR_WARNING
                ),
                ephemeral=True
            )
            return

        await self.bot.db.update_guild_settings(str(interaction.guild.id), update)

        embed = discord.Embed(
            title="✅ XP Settings Updated",
            description="\n".join(desc_parts),
            color=Config.COLOR_SUCCESS
        )
        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────
    # ROLE REWARDS
    # ─────────────────────────────────────────

    @app_commands.command(name="setrole", description="Set a role reward for reaching a level")
    @app_commands.describe(level="The level to trigger the role reward", role="The role to assign")
    @is_admin()
    async def set_role(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, 100],
        role: discord.Role
    ):
        # Validate bot can assign this role
        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Role Too High",
                    description="I cannot assign roles that are at or above my highest role.",
                    color=Config.COLOR_ERROR
                ),
                ephemeral=True
            )
            return

        await self.bot.db.set_role_reward(str(interaction.guild.id), level, str(role.id))

        embed = discord.Embed(
            title="✅ Role Reward Set",
            description=f"Reaching **Level {level}** will now assign {role.mention}",
            color=Config.COLOR_SUCCESS
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="removerole", description="Remove a role reward from a level")
    @app_commands.describe(level="The level to remove the reward from")
    @is_admin()
    async def remove_role(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, 100]
    ):
        await self.bot.db.remove_role_reward(str(interaction.guild.id), level)

        embed = discord.Embed(
            title="✅ Role Reward Removed",
            description=f"Removed role reward for **Level {level}**",
            color=Config.COLOR_SUCCESS
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rolerewards", description="View all configured role rewards")
    @is_admin()
    async def role_rewards_list(self, interaction: discord.Interaction):
        rewards = await self.bot.db.get_role_rewards(str(interaction.guild.id))

        embed = discord.Embed(
            title="🎖️ Role Rewards",
            color=Config.COLOR_PRIMARY
        )

        if not rewards:
            embed.description = "No role rewards configured. Use `/setrole` to add one."
        else:
            lines = []
            for r in rewards:
                role = interaction.guild.get_role(int(r["role_id"]))
                role_str = role.mention if role else f"`Deleted Role ({r['role_id']})`"
                lines.append(f"**Level {r['level']}** → {role_str}")
            embed.description = "\n".join(lines)

        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────
    # WELCOME SETTINGS
    # ─────────────────────────────────────────

    @app_commands.command(name="setwelcome", description="Set the welcome/goodbye channel")
    @app_commands.describe(channel="Channel for welcome messages (leave empty to disable)")
    @is_admin()
    async def set_welcome(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel = None
    ):
        channel_id = str(channel.id) if channel else None
        await self.bot.db.update_guild_settings(
            str(interaction.guild.id),
            {"welcome_channel": channel_id}
        )

        if channel:
            desc = f"Welcome messages will be sent to {channel.mention}"
        else:
            desc = "Welcome messages have been **disabled**."

        embed = discord.Embed(
            title="✅ Welcome Channel Updated",
            description=desc,
            color=Config.COLOR_SUCCESS
        )
        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────
    # LOG CHANNEL
    # ─────────────────────────────────────────

    @app_commands.command(name="setlogchannel", description="Set the moderation log channel")
    @app_commands.describe(channel="Channel for moderation logs")
    @is_admin()
    async def set_log_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel
    ):
        await self.bot.db.update_guild_settings(
            str(interaction.guild.id),
            {"log_channel": str(channel.id)}
        )
        embed = discord.Embed(
            title="✅ Log Channel Set",
            description=f"Moderation logs will be sent to {channel.mention}",
            color=Config.COLOR_SUCCESS
        )
        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────
    # VOICE XP TOGGLE
    # ─────────────────────────────────────────

    @app_commands.command(name="setvoicexp", description="Enable or disable voice channel XP")
    @app_commands.describe(
        enabled="Enable voice XP",
        amount="XP per 5-minute interval (1-50)"
    )
    @is_admin()
    async def set_voice_xp(
        self,
        interaction: discord.Interaction,
        enabled: bool = None,
        amount: app_commands.Range[int, 1, 50] = None
    ):
        update = {}
        desc_parts = []

        if enabled is not None:
            update["voice_xp_enabled"] = enabled
            status = "**enabled**" if enabled else "**disabled**"
            desc_parts.append(f"Voice XP: {status}")

        if amount is not None:
            update["voice_xp_amount"] = amount
            desc_parts.append(f"XP per interval: **{amount}**")

        if not update:
            await interaction.response.send_message("Provide at least one option.", ephemeral=True)
            return

        await self.bot.db.update_guild_settings(str(interaction.guild.id), update)

        embed = discord.Embed(
            title="✅ Voice XP Updated",
            description="\n".join(desc_parts),
            color=Config.COLOR_SUCCESS
        )
        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────
    # REACTION XP TOGGLE
    # ─────────────────────────────────────────

    @app_commands.command(name="setreactionxp", description="Enable or disable reaction XP")
    @app_commands.describe(enabled="Enable reaction XP")
    @is_admin()
    async def set_reaction_xp(
        self,
        interaction: discord.Interaction,
        enabled: bool
    ):
        await self.bot.db.update_guild_settings(
            str(interaction.guild.id),
            {"reaction_xp_enabled": enabled}
        )
        status = "**enabled**" if enabled else "**disabled**"
        embed = discord.Embed(
            title="✅ Reaction XP Updated",
            description=f"Reaction XP is now {status}",
            color=Config.COLOR_SUCCESS
        )
        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────
    # COOLDOWN SETTINGS
    # ─────────────────────────────────────────

    @app_commands.command(name="setcooldown", description="Set XP message cooldown in seconds")
    @app_commands.describe(seconds="Cooldown between XP awards (10-600 seconds)")
    @is_admin()
    async def set_cooldown(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 10, 600]
    ):
        await self.bot.db.update_guild_settings(
            str(interaction.guild.id),
            {"xp_cooldown": seconds}
        )
        embed = discord.Embed(
            title="✅ Cooldown Updated",
            description=f"XP cooldown set to **{seconds} seconds**",
            color=Config.COLOR_SUCCESS
        )
        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────
    # SETTINGS OVERVIEW
    # ─────────────────────────────────────────

    @app_commands.command(name="settings", description="View all bot settings for this server")
    @is_admin()
    async def settings(self, interaction: discord.Interaction):
        """Display all current bot settings."""
        guild_id = str(interaction.guild.id)
        settings = await self.bot.db.get_guild_settings(guild_id)

        def get_channel(channel_id):
            if not channel_id:
                return "Not set"
            ch = interaction.guild.get_channel(int(channel_id))
            return ch.mention if ch else "Deleted channel"

        embed = discord.Embed(
            title=f"⚙️ Settings — {interaction.guild.name}",
            color=Config.COLOR_INFO
        )
        embed.add_field(
            name="📊 XP Settings",
            value=(
                f"XP per message: **{settings.get('xp_per_message', 15)}**\n"
                f"Cooldown: **{settings.get('xp_cooldown', 60)}s**"
            ),
            inline=False
        )
        embed.add_field(
            name="📣 Channels",
            value=(
                f"Level-up: {get_channel(settings.get('level_up_channel'))}\n"
                f"Welcome: {get_channel(settings.get('welcome_channel'))}\n"
                f"Mod log: {get_channel(settings.get('log_channel'))}"
            ),
            inline=False
        )
        embed.add_field(
            name="🎙️ Voice XP",
            value=(
                f"Enabled: **{'✅' if settings.get('voice_xp_enabled', True) else '❌'}**\n"
                f"XP per interval: **{settings.get('voice_xp_amount', 10)}**"
            ),
            inline=True
        )
        embed.add_field(
            name="⭐ Reaction XP",
            value=f"Enabled: **{'✅' if settings.get('reaction_xp_enabled', True) else '❌'}**",
            inline=True
        )
        embed.set_footer(text="Use admin commands to change any setting")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────
    # RESET USER XP (Admin tool)
    # ─────────────────────────────────────────

    @app_commands.command(name="resetxp", description="Reset a user's XP (admin only)")
    @app_commands.describe(member="The member to reset XP for")
    @is_admin()
    async def reset_xp(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ):
        await self.bot.db.users.update_one(
            {"guild_id": str(interaction.guild.id), "user_id": str(member.id)},
            {"$set": {"xp": 0, "level": 0, "messages": 0, "voice_minutes": 0}}
        )
        embed = discord.Embed(
            title="✅ XP Reset",
            description=f"Reset all XP and stats for {member.mention}",
            color=Config.COLOR_SUCCESS
        )
        await interaction.response.send_message(embed=embed)
    @app_commands.command(name="sendmessage", description="Send a message or embed to any channel as the bot")
    @app_commands.describe(
        channel="The channel to send the message to",
        message="The message content (supports markdown, mentions, emojis)",
        title="Optional embed title (if set, message becomes embed description)",
        image_url="Optional image URL to attach to the embed"
    )
    @is_admin()
    async def sendmessage(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str,
        title: str = None,
        image_url: str = None,
    ):
        try:
            if title or image_url:
                # Send as embed
                embed = discord.Embed(
                    title=title or None,
                    description=message,
                    color=Config.COLOR_PRIMARY,
                )
                if image_url:
                    embed.set_image(url=image_url)

                await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=True, roles=True, users=True,
                    ),
                )
            else:
                # Plain text message
                await channel.send(
                    message,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=True, roles=True, users=True,
                    ),
                )

            await interaction.response.send_message(
                "\u2705 Message sent successfully.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "\u274c I don't have permission to send messages in that channel.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.response.send_message(
                "\u274c Failed to send message: `{}`".format(e),
                ephemeral=True,
            )

async def setup(bot):
    await bot.add_cog(Admin(bot))
