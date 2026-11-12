# -*- coding: utf-8 -*-
"""
WHERE WINDS MEET - Welcome Cog (DM Edition)
Sends a private DM to every new member. No public channel messages.
"""

import logging
from datetime import datetime

import discord
from discord.ext import commands

logger = logging.getLogger("wwm.welcome")

# --- Edit these links to match your server ---
ANNOUNCEMENTS_URL = "https://discord.com/channels/1473168406202159146/1476206073903054971"
EVENTS_URL        = "https://discord.com/channels/1473168406202159146/1473521726569844858"
BEGINNERS_URL     = "https://discord.com/channels/1473168406202159146/1473168407170912298"
TEAHOUSE_URL      = "https://discord.com/channels/1473168406202159146/1473596358165266625"
MOBILE_DOWNLOAD   = "http://bit.ly/49ErYfg"
PC_DOWNLOAD       = "https://wherewindsmeetgame.com/"
# ---------------------------------------------


class WelcomeLinks(discord.ui.View):
    """Six link-buttons attached to the welcome DM."""

    def __init__(self):
        super().__init__(timeout=None)

        self.add_item(discord.ui.Button(
            label="Mobile",
            url=MOBILE_DOWNLOAD,
            style=discord.ButtonStyle.link,
            row=0,
            emoji="\U0001f4f1",
        ))
        self.add_item(discord.ui.Button(
            label="PC / PS5",
            url=PC_DOWNLOAD,
            style=discord.ButtonStyle.link,
            row=0,
            emoji="\U0001f4bb",
        ))
        self.add_item(discord.ui.Button(
            label="Announcements",
            url=ANNOUNCEMENTS_URL,
            style=discord.ButtonStyle.link,
            row=1,
            emoji="\U0001f4e2",
        ))
        self.add_item(discord.ui.Button(
            label="Events",
            url=EVENTS_URL,
            style=discord.ButtonStyle.link,
            row=1,
            emoji="\U0001f389",
        ))
        self.add_item(discord.ui.Button(
            label="Teahouse Chat",
            url=TEAHOUSE_URL,
            style=discord.ButtonStyle.link,
            row=1,
            emoji="\u2694\ufe0f",
        ))


def build_welcome_embed(member: discord.Member) -> discord.Embed:
    """Build the premium welcome embed."""
    embed = discord.Embed(
        title="\U0001f4dc Welcome to the official Where Winds Meet Discord!",
        description=(
            "### Welcome, **{}**!\n"
            "Here's your **Starter Kit** to help you navigate the vast "
            "open world and master legendary martial arts."
        ).format(member.display_name),
        color=0x00C8FF,
        timestamp=datetime.utcnow(),
    )

    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(
        name="\U0001f4e2 Keep Up with the Jianghu",
        value=(
            "[**Go to Announcements**]({})\n"
            "*Don't miss out on game updates and patch notes.*"
        ).format(ANNOUNCEMENTS_URL),
        inline=False,
    )
    embed.add_field(
        name="\U0001f389 Join Community Events",
        value=(
            "[**Go to Events**]({})\n"
            "*Join community events and win wuxia treasures!*"
        ).format(EVENTS_URL),
        inline=False,
    )
    embed.add_field(
        name="\U0001f4da Beginner Questions",
        value=(
            "[**Go to Beginner Chat**]({})\n"
            "*This is your friendly space for all things beginner-related!*"
        ).format(BEGINNERS_URL),
        inline=False,
    )
    embed.add_field(
        name="\u2694\ufe0f Teahouse Chat",
        value=(
            "[**Go to Teahouse**]({})\n"
            "*Discuss , find a party for dungeons, or share your adventure.*"
        ).format(TEAHOUSE_URL),
        inline=False,
    )
    embed.add_field(
        name="\U0001f517 Important Links",
        value=(
            "*Download the Game:* \U0001f4f1 [**Mobile**]({}) "
            "& \U0001f4bb [**PC / PS5**]({})"
        ).format(MOBILE_DOWNLOAD, PC_DOWNLOAD),
        inline=False,
    )

    embed.set_footer(
        text="Where Winds Meet \u2022 {}".format(member.guild.name),
        icon_url=member.guild.icon.url if member.guild.icon else None,
    )
    return embed


class Welcome(commands.Cog):
    """Welcome cog - DM only, no public channel messages."""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Send a private DM welcome when a member joins."""
        if member.bot:
            return

        logger.info("New member: %s (%s) in %s", member, member.id, member.guild.name)

        # Init user in DB
        await self.bot.db.create_user(str(member.guild.id), str(member.id))

        embed = build_welcome_embed(member)
        view  = WelcomeLinks()

        try:
            await member.send(embed=embed, view=view)
            logger.info("Welcome DM sent to %s", member)
        except discord.Forbidden:
            # User has DMs disabled - silent skip
            logger.warning("Could not DM %s (%s) - DMs disabled", member, member.id)
        except discord.HTTPException as e:
            logger.error("Failed to send welcome DM to %s: %s", member, e)


async def setup(bot):
    await bot.add_cog(Welcome(bot))
