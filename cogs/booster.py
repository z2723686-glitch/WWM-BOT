# -*- coding: utf-8 -*-
"""
WWM - Server Booster Thank-You System
Fires on_member_update: when premiumSince changes from None -> a date,
sends a decorated thank-you embed to the boost channel.
"""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from config import Config

logger = logging.getLogger("wwm.booster")

# ── Config ────────────────────────────────────────────────────────────────────
BOOST_CHANNEL_ID = 1524393452723241050
BOOST_BANNER_URL = "https://i.ibb.co/G3cgmWXR/3486daefd3.jpg"

# Colour: Discord boost pink
BOOST_COLOR = 0xF47FFF


class Booster(commands.Cog):
    """Sends a thank-you message when a member boosts the server."""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ):
        # Only fire when premiumSince goes from None -> a datetime (new boost)
        if before.premium_since is not None or after.premium_since is None:
            return

        channel = after.guild.get_channel(BOOST_CHANNEL_ID)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(BOOST_CHANNEL_ID)
            except Exception as e:
                logger.error("Cannot fetch boost channel %s: %s", BOOST_CHANNEL_ID, e)
                return

        logger.info(
            "%s (%s) just boosted %s",
            after, after.id, after.guild.name
        )

        embed = discord.Embed(
            title="\U0001f48e New Server Boost has been Added!",
            description=(
                "Thank you so much {} for **Boosting** our server! \U0001f496\n\n"
                "Welcome to our **Booster Army**!\n\n"
                "\u2764\ufe0f Take Much Much Love!\n\n"
                "\U0001f381 You have received your **Server Booster** role.\n\n"
                "Thanks for supporting the server!"
            ).format(after.mention),
            color=BOOST_COLOR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=after.display_avatar.url)
        embed.set_image(url=BOOST_BANNER_URL)

        try:
            await channel.send(
                content="\U0001f389 Everyone welcome our new booster!",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.Forbidden:
            logger.error(
                "Missing permission to send in boost channel %s. "
                "Grant Send Messages + Embed Links to the bot in that channel.",
                BOOST_CHANNEL_ID,
            )
        except Exception as e:
            logger.error("Failed to send boost message: %s", e)


async def setup(bot):
    await bot.add_cog(Booster(bot))
