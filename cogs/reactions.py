"""
WHERE WINDS MEET - Reaction XP Cog
=====================================
Awards XP for giving and receiving reactions.
Anti-abuse: no self-reaction XP, cooldowns, duplicate prevention.
"""

import logging
from datetime import datetime

import discord
from discord.ext import commands

from config import Config
from utils.xp_system import calculate_level_from_xp
from utils.role_rewards import process_role_rewards

logger = logging.getLogger("wwm.reactions")

# Reactions that grant XP
SUPPORTED_REACTIONS = ["❤️", "👍", "🔥", "😂", "😍", "⭐", "💯"]


class Reactions(commands.Cog):
    """Reaction-based XP system with anti-abuse protection."""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """
        Handle reaction additions.
        Awards XP to both giver and receiver.
        """
        # Ignore DMs
        if not payload.guild_id:
            return

        # Ignore bots
        if payload.member and payload.member.bot:
            return

        # Check if it's a supported reaction
        emoji_str = str(payload.emoji)
        if emoji_str not in SUPPORTED_REACTIONS:
            return

        guild_id = str(payload.guild_id)
        giver_id = str(payload.user_id)
        message_id = str(payload.message_id)

        # Check if reaction XP is enabled
        settings = await self.bot.db.get_guild_settings(guild_id)
        if not settings.get("reaction_xp_enabled", True):
            return

        # Fetch the message to find the receiver
        try:
            guild = self.bot.get_guild(payload.guild_id)
            channel = guild.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
        except Exception as e:
            logger.error(f"Failed to fetch message for reaction XP: {e}")
            return

        receiver_id = str(message.author.id)

        # No self-reactions
        if giver_id == receiver_id:
            return

        # Ignore bot messages
        if message.author.bot:
            return

        # Check for duplicate reaction XP (same message+giver+receiver)
        existing = await self.bot.db.reaction_logs.find_one({
            "guild_id": guild_id,
            "giver_id": giver_id,
            "receiver_id": receiver_id,
            "message_id": message_id
        })
        if existing:
            return  # Already gave XP for this reaction on this message

        # Check cooldown (same giver giving to same receiver)
        from datetime import timedelta
        cooldown_window = datetime.utcnow() - timedelta(
            seconds=Config.REACTION_XP_COOLDOWN
        )
        recent = await self.bot.db.reaction_logs.find_one({
            "guild_id": guild_id,
            "giver_id": giver_id,
            "receiver_id": receiver_id,
            "timestamp": {"$gte": cooldown_window}
        })
        if recent:
            return  # On cooldown

        # Log this reaction to prevent duplicates
        await self.bot.db.reaction_logs.insert_one({
            "guild_id": guild_id,
            "giver_id": giver_id,
            "receiver_id": receiver_id,
            "message_id": message_id,
            "emoji": emoji_str,
            "timestamp": datetime.utcnow()
        })

        # Award XP to both parties
        await self._award_xp(guild, guild_id, giver_id, Config.REACTION_XP_GIVE, "give")
        await self._award_xp(guild, guild_id, receiver_id, Config.REACTION_XP_RECEIVE, "receive")

    async def _award_xp(
        self,
        guild: discord.Guild,
        guild_id: str,
        user_id: str,
        xp_amount: int,
        action: str
    ):
        """Award reaction XP to a user."""
        try:
            user_data = await self.bot.db.get_user(guild_id, user_id)
            old_xp = user_data.get("xp", 0)
            old_level, _, _ = calculate_level_from_xp(old_xp)

            field = "reactions_given" if action == "give" else "reactions_received"

            await self.bot.db.users.update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {
                    "$inc": {"xp": xp_amount, field: 1},
                    "$setOnInsert": {"guild_id": guild_id, "user_id": user_id, "level": 0}
                },
                upsert=True
            )

            # Check level up
            new_xp = old_xp + xp_amount
            new_level, _, _ = calculate_level_from_xp(new_xp)

            if new_level > old_level:
                member = guild.get_member(int(user_id))
                if member:
                    await self.bot.db.users.update_one(
                        {"guild_id": guild_id, "user_id": user_id},
                        {"$set": {"level": new_level}}
                    )
                    await process_role_rewards(self.bot, member, old_level, new_level)

        except Exception as e:
            logger.error(f"Reaction XP error for user {user_id}: {e}")


async def setup(bot):
    await bot.add_cog(Reactions(bot))
