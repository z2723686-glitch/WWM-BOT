# -*- coding: utf-8 -*-
"""
WWM - Leveling Cog
Reads member.status in real-time and passes colour + name to rank card.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from utils.xp_system import (
    calculate_level_from_xp,
    get_random_xp,
    format_xp,
    format_voice_time,
)
from utils.role_rewards import process_role_rewards
from utils.rank_card import (
    generate_rank_card_with_avatar,
    STATUS_ONLINE,
    STATUS_IDLE,
    STATUS_DND,
    STATUS_OFFLINE,
)
from utils.permissions import in_level_channel, _is_owner, LEVEL_CHANNEL_ID

logger = logging.getLogger("wwm.leveling")


# ── Status helper ─────────────────────────────────────────────────────────────

def _get_status(member: discord.Member) -> tuple:
    """
    Returns (colour_tuple, status_name_str) for the member's current status.
    Requires PRESENCES intent to be enabled in Discord Developer Portal.
    Falls back to offline if intent is missing.
    """
    s = member.status

    if s == discord.Status.online:
        return STATUS_ONLINE, "online"
    elif s == discord.Status.idle:
        return STATUS_IDLE, "idle"
    elif s == discord.Status.dnd:
        return STATUS_DND, "dnd"
    elif s == discord.Status.invisible:
        return STATUS_OFFLINE, "invisible"   # invisible looks like offline
    else:
        return STATUS_OFFLINE, "offline"


# ── Cog ───────────────────────────────────────────────────────────────────────

class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot          = bot
        self._cooldowns   = {}
        self._msg_history = defaultdict(list)

    # ── Anti-abuse ────────────────────────────────────────────

    def _on_cooldown(self, gid, uid, secs):
        last = self._cooldowns.get((gid, uid))
        if last is None:
            return False
        return (datetime.utcnow() - last).total_seconds() < secs

    def _set_cooldown(self, gid, uid):
        self._cooldowns[(gid, uid)] = datetime.utcnow()

    def _is_spam(self, gid, uid):
        key    = (gid, uid)
        now    = datetime.utcnow()
        cutoff = now - timedelta(seconds=10)
        self._msg_history[key] = [t for t in self._msg_history[key] if t > cutoff]
        self._msg_history[key].append(now)
        return len(self._msg_history[key]) > 5

    # ── Message XP ───────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if message.type != discord.MessageType.default:
            return

        gid = str(message.guild.id)
        uid = str(message.author.id)

        settings   = await self.bot.db.get_guild_settings(gid)
        cooldown   = settings.get("xp_cooldown",    Config.DEFAULT_XP_COOLDOWN)
        xp_per_msg = settings.get("xp_per_message", Config.DEFAULT_XP_PER_MESSAGE)

        if self._on_cooldown(gid, uid, cooldown) or self._is_spam(gid, uid):
            return
        self._set_cooldown(gid, uid)

        xp_gain  = get_random_xp(xp_per_msg)
        user_doc = await self.bot.db.get_user(gid, uid)
        old_xp   = user_doc.get("xp", 0)
        old_lv, _, _ = calculate_level_from_xp(old_xp)

        await self.bot.db.users.update_one(
            {"guild_id": gid, "user_id": uid},
            {
                "$inc": {"xp": xp_gain, "messages": 1},
                "$setOnInsert": {
                    "guild_id": gid, "user_id": uid,
                    "level": 1, "voice_minutes": 0,
                },
            },
            upsert=True,
        )

        new_xp = old_xp + xp_gain
        new_lv, _, _ = calculate_level_from_xp(new_xp)
        if new_lv > old_lv:
            await self._level_up(message.guild, message.author,
                                  old_lv, new_lv, new_xp, settings)

    # ── Level-up ──────────────────────────────────────────────

    async def _level_up(self, guild, member, old_lv, new_lv, total_xp, settings):
        await self.bot.db.users.update_one(
            {"guild_id": str(guild.id), "user_id": str(member.id)},
            {"$set": {"level": new_lv}},
        )
        new_roles = await process_role_rewards(self.bot, member, old_lv, new_lv)

        ch_id   = settings.get("level_up_channel")
        channel = guild.get_channel(int(ch_id)) if ch_id else None
        if not channel:
            return

        embed = discord.Embed(
            title="LEVEL UP!",
            description="{} reached **Level {}**!".format(member.display_name, new_lv),
            color=Config.COLOR_PRIMARY,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Progress",
                        value="Level {} -> **Level {}**".format(old_lv, new_lv), inline=True)
        embed.add_field(name="Total XP",
                        value="**{}** XP".format(format_xp(total_xp)), inline=True)
        if new_roles:
            embed.add_field(name="New Role(s)!",
                            value=", ".join("`{}`".format(r.name) for r in new_roles),
                            inline=False)
        embed.set_footer(text="Where Winds Meet")
        try:
            await channel.send(
                content=member.mention, embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.Forbidden:
            pass

    # ── /rank ─────────────────────────────────────────────────

    @app_commands.command(name="rank", description="View your rank card and stats")
    @app_commands.describe(member="Member to check (leave empty for yourself)")
    @in_level_channel()
    async def rank(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer(thinking=True)

        target = member or interaction.user
        gid    = str(interaction.guild.id)
        uid    = str(target.id)

        doc   = await self.bot.db.get_user(gid, uid)
        total = doc.get("xp", 0)
        msgs  = doc.get("messages", 0)
        voice = doc.get("voice_minutes", 0)

        level, cur_xp, need_xp = calculate_level_from_xp(total)
        rank = await self.bot.db.get_user_rank(gid, uid)

        # ── Read real-time status ──────────────────────────────
        status_color, status_name = _get_status(target)

        try:
            buf = await generate_rank_card_with_avatar(
                username=target.name,
                discriminator="0",
                avatar_url=str(target.display_avatar.url),
                level=level,
                current_xp=cur_xp,
                needed_xp=need_xp,
                total_xp=total,
                rank=rank,
                messages=msgs,
                voice_minutes=voice,
                status_color=status_color,
                status_name=status_name,
            )
            file  = discord.File(buf, filename="rank_{}.png".format(uid))
            embed = discord.Embed(color=Config.COLOR_PRIMARY)
            embed.set_image(url="attachment://rank_{}.png".format(uid))
            embed.set_footer(text="Where Winds Meet")
            await interaction.followup.send(embed=embed, file=file)

        except Exception as e:
            logger.error("Rank card error: %s", e)
            embed = discord.Embed(
                title="{}'s Stats".format(target.display_name),
                color=Config.COLOR_PRIMARY,
            )
            embed.add_field(name="Rank",     value="#{}".format(rank),               inline=True)
            embed.add_field(name="Level",    value=str(level),                       inline=True)
            embed.add_field(name="XP",       value="{}/{}".format(cur_xp, need_xp),  inline=True)
            embed.add_field(name="Messages", value="{:,}".format(msgs),              inline=True)
            embed.add_field(name="Voice",    value=format_voice_time(voice),         inline=True)
            embed.add_field(name="Total XP", value=format_xp(total),                inline=True)
            await interaction.followup.send(embed=embed)

    # ── !rank prefix command ──────────────────────────────────

    @commands.command(name="rank")
    async def rank_prefix(self, ctx: commands.Context, member: discord.Member = None):
        """!rank / !rank @someone"""
        if not _is_owner(ctx.author.id) and ctx.channel.id != LEVEL_CHANNEL_ID:
            embed = discord.Embed(
                description=(
                    "\U0001f4ca This command only works in <#{}>."
                    " Head over there to check your level!"
                ).format(LEVEL_CHANNEL_ID),
                color=Config.COLOR_WARNING,
            )
            msg = await ctx.send(embed=embed)
            await asyncio.sleep(8)
            try:
                await msg.delete()
                await ctx.message.delete()
            except Exception:
                pass
            return

        target = member or ctx.author
        gid    = str(ctx.guild.id)
        uid    = str(target.id)

        async with ctx.typing():
            doc   = await self.bot.db.get_user(gid, uid)
            total = doc.get("xp", 0)
            msgs  = doc.get("messages", 0)
            voice = doc.get("voice_minutes", 0)

            level, cur_xp, need_xp = calculate_level_from_xp(total)
            rank = await self.bot.db.get_user_rank(gid, uid)

            # Real-time status (member from cache — ctx.guild.get_member is needed)
            target_member = ctx.guild.get_member(target.id) or target
            status_color, status_name = _get_status(target_member)

            try:
                buf = await generate_rank_card_with_avatar(
                    username=target.name,
                    discriminator="0",
                    avatar_url=str(target.display_avatar.url),
                    level=level,
                    current_xp=cur_xp,
                    needed_xp=need_xp,
                    total_xp=total,
                    rank=rank,
                    messages=msgs,
                    voice_minutes=voice,
                    status_color=status_color,
                    status_name=status_name,
                )
                file  = discord.File(buf, filename="rank_{}.png".format(uid))
                embed = discord.Embed(color=Config.COLOR_PRIMARY)
                embed.set_image(url="attachment://rank_{}.png".format(uid))
                embed.set_footer(text="Where Winds Meet")
                await ctx.send(embed=embed, file=file)

            except Exception as e:
                logger.error("!rank error: %s", e)
                embed = discord.Embed(
                    title="{}'s Stats".format(target.display_name),
                    color=Config.COLOR_PRIMARY,
                )
                embed.add_field(name="Rank",     value="#{}".format(rank),               inline=True)
                embed.add_field(name="Level",    value=str(level),                       inline=True)
                embed.add_field(name="XP",       value="{}/{}".format(cur_xp, need_xp),  inline=True)
                embed.add_field(name="Messages", value="{:,}".format(msgs),              inline=True)
                embed.add_field(name="Voice",    value=format_voice_time(voice),         inline=True)
                embed.add_field(name="Total XP", value=format_xp(total),                inline=True)
                await ctx.send(embed=embed)

    # ── /xp ──────────────────────────────────────────────────

    @app_commands.command(name="xp", description="Quick XP check")
    @in_level_channel()
    async def xp_check(self, interaction: discord.Interaction):
        gid   = str(interaction.guild.id)
        uid   = str(interaction.user.id)
        doc   = await self.bot.db.get_user(gid, uid)
        total = doc.get("xp", 0)
        level, cur, need = calculate_level_from_xp(total)
        pct  = cur / need if need else 1
        bar  = chr(9608) * int(20 * pct) + chr(9617) * (20 - int(20 * pct))
        embed = discord.Embed(
            title="{}'s XP".format(interaction.user.display_name),
            description="`{}` {:.1f}%".format(bar, pct * 100),
            color=Config.COLOR_PRIMARY,
        )
        embed.add_field(name="Level",    value="**{}**".format(level),           inline=True)
        embed.add_field(name="Progress", value="**{}** / {}".format(cur, need),  inline=True)
        embed.add_field(name="Total XP", value="**{}**".format(format_xp(total)), inline=True)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Leveling(bot))
