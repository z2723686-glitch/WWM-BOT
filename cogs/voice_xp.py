"""
WHERE WINDS MEET - Voice XP Cog
==================================
Awards XP to users in voice channels every 5 minutes.
Ignores AFK channels and muted/deafened users.
"""

import asyncio
import logging
from datetime import datetime

import discord
from discord.ext import commands, tasks

from config import Config
from utils.xp_system import calculate_level_from_xp
from utils.role_rewards import process_role_rewards

logger = logging.getLogger("wwm.voice_xp")


class VoiceXP(commands.Cog):
    """Voice channel XP system with anti-abuse detection."""

    def __init__(self, bot):
        self.bot = bot
        # Track when users joined voice: {(guild_id, user_id): join_timestamp}
        self._voice_sessions: dict = {}
        self.voice_xp_task.start()

    def cog_unload(self):
        self.voice_xp_task.cancel()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ):
        """Track voice channel joins and leaves."""
        if member.bot:
            return

        key = (str(member.guild.id), str(member.id))

        # User joined a voice channel
        if after.channel and not before.channel:
            self._voice_sessions[key] = datetime.utcnow()
            logger.debug(f"Voice join: {member} in {after.channel.name}")

        # User left voice channel
        elif before.channel and not after.channel:
            if key in self._voice_sessions:
                del self._voice_sessions[key]
                logger.debug(f"Voice leave: {member}")

    @tasks.loop(minutes=Config.VOICE_XP_INTERVAL)
    async def voice_xp_task(self):
        """Award XP to active voice users every 5 minutes."""
        for guild in self.bot.guilds:
            guild_id = str(guild.id)

            # Check if voice XP is enabled for this guild
            settings = await self.bot.db.get_guild_settings(guild_id)
            if not settings.get("voice_xp_enabled", True):
                continue

            voice_xp_amount = settings.get("voice_xp_amount", Config.VOICE_XP_AMOUNT)
            afk_channel = guild.afk_channel

            for vc in guild.voice_channels:
                # Skip AFK channel
                if afk_channel and vc.id == afk_channel.id:
                    continue

                for member in vc.members:
                    # Skip bots
                    if member.bot:
                        continue

                    # Skip muted users (prevents AFK XP farming)
                    voice_state = member.voice
                    if voice_state:
                        # Skip if self-muted, server-muted, or deafened
                        if voice_state.self_mute or voice_state.mute:
                            continue
                        if voice_state.self_deaf or voice_state.deaf:
                            continue

                    # Award XP
                    user_id = str(member.id)
                    await self._award_voice_xp(guild, member, guild_id, user_id, voice_xp_amount)

    async def _award_voice_xp(
        self,
        guild: discord.Guild,
        member: discord.Member,
        guild_id: str,
        user_id: str,
        xp_amount: int
    ):
        """Award voice XP to a user and handle level-ups."""
        try:
            # Get current user data
            user_data = await self.bot.db.get_user(guild_id, user_id)
            old_xp = user_data.get("xp", 0)
            old_level, _, _ = calculate_level_from_xp(old_xp)

            # Update XP and voice minutes
            await self.bot.db.users.update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "$inc": {
                        "xp": xp_amount,
                        "voice_minutes": Config.VOICE_XP_INTERVAL
                    },
                    "$setOnInsert": {
                        "guild_id": guild_id,
                        "user_id": user_id,
                        "level": 0,
                        "messages": 0
                    }
                },
                upsert=True
            )

            # Check for level up
            new_xp = old_xp + xp_amount
            new_level, _, _ = calculate_level_from_xp(new_xp)

            if new_level > old_level:
                await self.bot.db.users.update_one(
                    {"guild_id": guild_id, "user_id": user_id},
                    {"$set": {"level": new_level}}
                )

                # Process role rewards
                await process_role_rewards(self.bot, member, old_level, new_level)

                # Level up notification (uses configured channel)
                settings = await self.bot.db.get_guild_settings(guild_id)
                channel_id = settings.get("level_up_channel")
                if channel_id:
                    channel = guild.get_channel(int(channel_id))
                    if channel:
                        embed = discord.Embed(
                            title="🎙️ Voice Level Up!",
                            description=f"{member.mention} reached **Level {new_level}** from voice activity!",
                            color=Config.COLOR_PRIMARY
                        )
                        await channel.send(embed=embed)

        except Exception as e:
            logger.error(f"Voice XP error for {member}: {e}")

    @voice_xp_task.before_loop
    async def before_voice_xp(self):
        """Wait for bot to be ready before starting task."""
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(VoiceXP(bot))
