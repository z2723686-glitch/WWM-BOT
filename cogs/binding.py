# -*- coding: utf-8 -*-
"""
WWM - Account Binding & Giveaway System
=========================================
Part 1 : Account binding via button + modal
Part 2 : Auto giveaway (all bound accounts eligible)
Part 3 : Selected giveaway (fixed winner)
Part 4 : Admin commands  /bound  /boundinfo  /unbind
Part 5 : Security - dedup, persistent giveaway recovery on restart
Part 6 : All IDs read from Config (never hardcoded)

Integrates with existing bot.db, Config, logger, permissions.
Does NOT modify any other file.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import IndexModel, ASCENDING

from config import Config
from utils.permissions import is_admin

logger = logging.getLogger("wwm.binding")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime, style: str = "F") -> str:
    """Discord timestamp mention."""
    return "<t:{}:{}>".format(int(dt.timestamp()), style)


def _parse_duration(raw: str) -> Optional[int]:
    """
    Parse a duration string like '30m', '1h', '2d' into total seconds.
    Returns None if unrecognised.
    """
    units = {"m": 60, "h": 3600, "d": 86400}
    raw = raw.strip().lower()
    if raw and raw[-1] in units:
        try:
            return int(raw[:-1]) * units[raw[-1]]
        except ValueError:
            pass
    return None


async def _send_log(
    bot: commands.Bot,
    embed: discord.Embed,
) -> None:
    """Send an embed to the configured BIND_LOG_CHANNEL."""
    log_ch_id = getattr(Config, "BIND_LOG_CHANNEL", 0)
    if not log_ch_id:
        return
    try:
        ch = bot.get_channel(log_ch_id) or await bot.fetch_channel(log_ch_id)
        await ch.send(embed=embed)
    except Exception as e:
        logger.warning("Could not send to BIND_LOG_CHANNEL: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# PART 1 — Account Binding
# ─────────────────────────────────────────────────────────────────────────────

class BindModal(discord.ui.Modal, title="Bind Your Account"):
    """Modal that collects the user's WWM UID."""

    wwm_uid = discord.ui.TextInput(
        label="WWM UID",
        placeholder="Enter your in-game UID (numbers only, max 20 digits)",
        required=True,
        max_length=20,
    )

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        uid  = self.wwm_uid.value.strip()
        user = interaction.user
        db   = self.bot.db

        # ── Validate: digits only ─────────────────────────────────────────────
        if not uid.isdigit():
            await interaction.followup.send(
                embed=discord.Embed(
                    description="\u274c **Invalid UID.** UID must contain numbers only.",
                    color=Config.COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        # ── Validate: Discord account not already bound ───────────────────────
        if await db.bound_accounts.find_one({"discord_id": str(user.id)}):
            await interaction.followup.send(
                embed=discord.Embed(
                    description="\u274c **You have already bound a game account.**",
                    color=Config.COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        # ── Validate: UID not already bound ──────────────────────────────────
        if await db.bound_accounts.find_one({"uid": uid}):
            await interaction.followup.send(
                embed=discord.Embed(
                    description="\u274c **This WWM UID has already been bound.**",
                    color=Config.COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        # ── Save ──────────────────────────────────────────────────────────────
        bound_at = _now()
        await db.bound_accounts.insert_one({
            "discord_id":           str(user.id),
            "discord_username":     user.name,
            "discord_display_name": user.display_name,
            "uid":                  uid,
            "guild_id":             str(interaction.guild_id),
            "bound_at":             bound_at,
        })

        logger.info("Account bound: %s (%s) -> UID %s", user, user.id, uid)

        # ── Confirm to user ───────────────────────────────────────────────────
        await interaction.followup.send(
            embed=discord.Embed(
                title="\u2705 Account Linked",
                description=(
                    "Your WWM account has been **successfully linked**.\n\n"
                    "**WWM UID:** `{}`\n"
                    "**Linked At:** {}"
                ).format(uid, _ts(bound_at)),
                color=Config.COLOR_SUCCESS,
            ),
            ephemeral=True,
        )

        # ── Log embed ─────────────────────────────────────────────────────────
        log_embed = discord.Embed(
            title="\U0001f517 New Account Bound",
            color=Config.COLOR_INFO,
            timestamp=bound_at,
        )
        log_embed.add_field(name="\U0001f464 Discord User",  value=user.mention,       inline=True)
        log_embed.add_field(name="\U0001f194 Discord ID",    value="`{}`".format(user.id), inline=True)
        log_embed.add_field(name="\U0001f4db Username",      value=user.name,           inline=True)
        log_embed.add_field(name="\U0001f3ae WWM UID",       value="`{}`".format(uid),  inline=True)
        log_embed.add_field(name="\U0001f55b Bound At",      value=_ts(bound_at),       inline=True)
        log_embed.set_thumbnail(url=user.display_avatar.url)
        log_embed.set_footer(text="WWM Account Binding System")
        await _send_log(self.bot, log_embed)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("BindModal error: %s", error)
        try:
            await interaction.followup.send(
                "An unexpected error occurred. Please try again.", ephemeral=True
            )
        except Exception:
            pass


class BindPanelView(discord.ui.View):
    """Persistent view with the Bind Account button."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Bind Account",
        style=discord.ButtonStyle.primary,
        emoji="\U0001f517",
        custom_id="wwm_bind:bind_account",
    )
    async def bind_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(BindModal(self.bot))


# ─────────────────────────────────────────────────────────────────────────────
# PART 2 & 3 — Giveaway
# ─────────────────────────────────────────────────────────────────────────────

class GiveawayView(discord.ui.View):
    """
    Placeholder persistent view for giveaway messages.
    No buttons — just keeps the message registered so we
    can edit it after the timer ends even after a restart.
    custom_id encodes the giveaway doc _id.
    """

    def __init__(self, giveaway_id: str) -> None:
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id


async def _run_giveaway(bot: commands.Bot, giveaway_doc: dict) -> None:
    """
    Core giveaway runner. Waits until end_time, then picks a winner
    and announces. Called both on creation and on restart recovery.
    """
    from bson import ObjectId

    giveaway_id = str(giveaway_doc["_id"])
    end_time    = giveaway_doc["end_time"]
    prize       = giveaway_doc["prize"]
    channel_id  = int(giveaway_doc["channel_id"])
    msg_id      = giveaway_doc.get("message_id")
    selected_id = giveaway_doc.get("selected_discord_id")  # for /sgiveaway

    # ── Wait until end time ───────────────────────────────────────────────────
    now        = _now()
    wait_secs  = (end_time - now).total_seconds()
    if wait_secs > 0:
        await asyncio.sleep(wait_secs)

    # ── Check not already finished (safety for restart race condition) ────────
    doc = await bot.db.giveaways.find_one({"_id": ObjectId(giveaway_id)})
    if not doc or doc.get("finished"):
        return

    # ── Mark finished immediately to prevent double-run ───────────────────────
    await bot.db.giveaways.update_one(
        {"_id": ObjectId(giveaway_id)},
        {"$set": {"finished": True, "finished_at": _now()}},
    )

    # ── Pick winner ───────────────────────────────────────────────────────────
    winner_doc = None

    if selected_id:
        # /sgiveaway — fixed winner
        winner_doc = await bot.db.bound_accounts.find_one(
            {"discord_id": selected_id}
        )
    else:
        # /giveaway — random from all bound accounts
        count = await bot.db.bound_accounts.count_documents({})
        if count == 0:
            winner_doc = None
        else:
            skip = random.randint(0, count - 1)
            cursor = bot.db.bound_accounts.find({}).skip(skip).limit(1)
            docs = await cursor.to_list(length=1)
            winner_doc = docs[0] if docs else None

    # ── Fetch the giveaway message to edit ───────────────────────────────────
    channel = bot.get_channel(channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.error("Cannot fetch giveaway channel %s: %s", channel_id, e)
            return

    # ── Build result embed ────────────────────────────────────────────────────
    if winner_doc:
        winner_discord_id = winner_doc["discord_id"]
        winner_uid        = winner_doc["uid"]
        winner_username   = winner_doc.get("discord_username", "Unknown")

        result_embed = discord.Embed(
            title="\U0001f3c6 Giveaway Ended",
            color=Config.COLOR_PRIMARY,
            timestamp=_now(),
        )
        result_embed.add_field(
            name="\U0001f3c6 Winner",
            value="<@{}> (`{}`)".format(winner_discord_id, winner_discord_id),
            inline=False,
        )
        result_embed.add_field(name="\U0001f4db Username", value=winner_username,    inline=True)
        result_embed.add_field(name="\U0001f3ae WWM UID",  value="`{}`".format(winner_uid), inline=True)
        result_embed.add_field(name="\U0001f381 Prize",    value="**{}**".format(prize),    inline=False)
        result_embed.set_footer(text="\U0001f389 Congratulations!")

        announce_content = (
            "\U0001f3c6 Congratulations <@{}>! "
            "You won **{}**!".format(winner_discord_id, prize)
        )
    else:
        result_embed = discord.Embed(
            title="\U0001f3c6 Giveaway Ended",
            description="\u274c No eligible participants found.",
            color=Config.COLOR_ERROR,
            timestamp=_now(),
        )
        announce_content = "\U0001f3c6 Giveaway ended — no eligible participants."

    # ── Edit original giveaway message ───────────────────────────────────────
    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            active_embed = msg.embeds[0] if msg.embeds else discord.Embed()
            active_embed.title       = "\U0001f3c6 Giveaway Ended"
            active_embed.description = (
                active_embed.description or ""
            ).replace("\u23f3", "\u2705")
            active_embed.color       = 0x95a5a6
            await msg.edit(embed=active_embed, view=None)
        except Exception as e:
            logger.warning("Could not edit giveaway message: %s", e)

    # ── Send result ───────────────────────────────────────────────────────────
    try:
        await channel.send(
            content=announce_content,
            embed=result_embed,
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except Exception as e:
        logger.error("Failed to send giveaway result: %s", e)

    logger.info(
        "Giveaway ended: prize='%s' winner=%s uid=%s",
        prize,
        winner_doc.get("discord_id") if winner_doc else "none",
        winner_doc.get("uid")        if winner_doc else "none",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main Cog
# ─────────────────────────────────────────────────────────────────────────────

class Binding(commands.Cog):
    """Account binding + giveaway system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot              = bot
        self._giveaway_tasks: dict[str, asyncio.Task] = {}

    async def cog_load(self) -> None:
        # Re-register persistent bind panel view
        self.bot.add_view(BindPanelView(self.bot))

        # Create MongoDB indexes
        try:
            await self.bot.db.bound_accounts.create_indexes([
                IndexModel([("discord_id", ASCENDING)], unique=True, name="ba_discord_unique"),
                IndexModel([("uid",        ASCENDING)], unique=True, name="ba_uid_unique"),
                IndexModel([("guild_id",   ASCENDING)],              name="ba_guild"),
            ])
            await self.bot.db.giveaways.create_indexes([
                IndexModel([("finished",  ASCENDING)], name="ga_finished"),
                IndexModel([("end_time",  ASCENDING)], name="ga_end_time"),
            ])
            logger.info("Binding: MongoDB indexes ensured.")
        except Exception as e:
            logger.warning("Binding index creation: %s", e)

        # Resume any unfinished giveaways from before a restart
        await self._resume_giveaways()

    async def _resume_giveaways(self) -> None:
        """Restart timers for all giveaways that weren't finished."""
        try:
            cursor = self.bot.db.giveaways.find({"finished": False})
            count  = 0
            async for doc in cursor:
                gid  = str(doc["_id"])
                task = asyncio.create_task(_run_giveaway(self.bot, doc))
                self._giveaway_tasks[gid] = task
                count += 1
            if count:
                logger.info("Binding: resumed %d unfinished giveaway(s).", count)
        except Exception as e:
            logger.error("Failed to resume giveaways: %s", e)

    def cog_unload(self) -> None:
        for task in self._giveaway_tasks.values():
            task.cancel()

    # ── /bindpanel ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="bindpanel",
        description="Post the permanent Account Binding panel",
    )
    @is_admin()
    async def bindpanel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        ch_id = getattr(Config, "BIND_CHANNEL", 0)
        if not ch_id:
            await interaction.followup.send(
                "**BIND_CHANNEL** is not set in environment variables.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(ch_id)
        if not channel:
            await interaction.followup.send(
                "Channel `{}` not found. Check **BIND_CHANNEL**.".format(ch_id),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="\U0001f381 Bind Your Game Account",
            description=(
                "**WWM Account Binding**.\n\n"
                "Follow these steps:\n\n"
                "**1.** Click the **Bind Account** button below.\n"
                "**2.** Enter your **WWM UID**.\n"
                "**3.** Submit the form.\n"
                "**4.** Your account will be permanently linked to your Discord account.\n\n"
                "\u26a0\ufe0f Each Discord account can only bind **one** WWM account.\n"
                "\u26a0\ufe0f Each WWM UID can only be bound **once**."
            ),
            color=Config.COLOR_PRIMARY,
        )
        embed.set_footer(text="Where Winds Meet \u2022 Account Binding System")

        view = BindPanelView(self.bot)
        await channel.send(embed=embed, view=view)

        await interaction.followup.send(
            embed=discord.Embed(
                title="\u2705 Bind Panel Posted",
                description="Panel posted in <#{}>.".format(ch_id),
                color=Config.COLOR_SUCCESS,
            ),
            ephemeral=True,
        )

    # ── /giveaway ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="giveaway",
        description="Start a giveaway — all bound accounts are eligible",
    )
    @app_commands.describe(
        channel="Channel to post the giveaway in",
        duration="Duration: 30m / 1h / 2h / 6h / 12h / 1d / 2d / 7d",
        prize="What are you giving away?",
    )
    @is_admin()
    async def giveaway(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        duration: str,
        prize: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        secs = _parse_duration(duration)
        if not secs:
            await interaction.followup.send(
                "\u274c Invalid duration. Use: `30m` `1h` `2h` `6h` `12h` `1d` `2d` `7d`",
                ephemeral=True,
            )
            return

        end_time    = _now() + timedelta(seconds=secs)
        participant = await self.bot.db.bound_accounts.count_documents({})

        embed = discord.Embed(
            title="\U0001f389 GIVEAWAY",
            description="\u23f3 React and wait — winner announced automatically!",
            color=Config.COLOR_PRIMARY,
            timestamp=end_time,
        )
        embed.add_field(name="\U0001f381 Prize",        value="**{}**".format(prize),    inline=False)
        embed.add_field(name="\u23f1\ufe0f Duration",   value=duration.upper(),           inline=True)
        embed.add_field(name="\U0001f4c5 Ends",         value=_ts(end_time),              inline=True)
        embed.add_field(name="\U0001f465 Participants", value=str(participant),           inline=True)
        embed.set_footer(text="Eligible: All bound accounts \u2022 Ends")

        msg = await channel.send(embed=embed)

        # ── Persist to MongoDB ────────────────────────────────────────────────
        doc = {
            "prize":                prize,
            "channel_id":           str(channel.id),
            "message_id":           str(msg.id),
            "end_time":             end_time,
            "started_by":           str(interaction.user.id),
            "selected_discord_id":  None,
            "finished":             False,
            "created_at":           _now(),
        }
        result = await self.bot.db.giveaways.insert_one(doc)
        doc["_id"] = result.inserted_id
        gid = str(result.inserted_id)

        # ── Start timer task ──────────────────────────────────────────────────
        task = asyncio.create_task(_run_giveaway(self.bot, doc))
        self._giveaway_tasks[gid] = task

        await interaction.followup.send(
            embed=discord.Embed(
                title="\u2705 Giveaway Started",
                description=(
                    "Giveaway for **{}** started in <#{}>.\n"
                    "Ends: {}"
                ).format(prize, channel.id, _ts(end_time)),
                color=Config.COLOR_SUCCESS,
            ),
            ephemeral=True,
        )
        logger.info(
            "Giveaway started by %s: prize='%s' channel=%s ends=%s",
            interaction.user, prize, channel.id, end_time,
        )

    # ── /sgiveaway (selected winner) ─────────────────────────────────────────

    @app_commands.command(
        name="sgiveaway",
        description="Start a giveaway with a pre-selected winner",
    )
    @app_commands.describe(
        channel="Channel to post the giveaway in",
        duration="Duration: 30m / 1h / 2h / 6h / 12h / 1d / 2d / 7d",
        selected_user="The member who will win",
        prize="What are you giving away?",
    )
    @is_admin()
    async def sgiveaway(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        duration: str,
        selected_user: discord.Member,
        prize: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        secs = _parse_duration(duration)
        if not secs:
            await interaction.followup.send(
                "\u274c Invalid duration. Use: `30m` `1h` `2h` `6h` `12h` `1d` `2d` `7d`",
                ephemeral=True,
            )
            return

        # Verify selected user is bound
        bound = await self.bot.db.bound_accounts.find_one(
            {"discord_id": str(selected_user.id)}
        )
        if not bound:
            await interaction.followup.send(
                "\u274c **{}** has not bound a game account. "
                "They must bind before being selected as a winner.".format(
                    selected_user.display_name
                ),
                ephemeral=True,
            )
            return

        end_time    = _now() + timedelta(seconds=secs)
        participant = await self.bot.db.bound_accounts.count_documents({})

        embed = discord.Embed(
            title="\U0001f389 GIVEAWAY",
            description="\u23f3 Special giveaway — winner announced automatically!",
            color=Config.COLOR_PRIMARY,
            timestamp=end_time,
        )
        embed.add_field(name="\U0001f381 Prize",        value="**{}**".format(prize), inline=False)
        embed.add_field(name="\u23f1\ufe0f Duration",   value=duration.upper(),        inline=True)
        embed.add_field(name="\U0001f4c5 Ends",         value=_ts(end_time),           inline=True)
        embed.add_field(name="\U0001f465 Participants", value=str(participant),        inline=True)
        embed.set_footer(text="Eligible: All bound accounts \u2022 Ends")

        msg = await channel.send(embed=embed)

        doc = {
            "prize":               prize,
            "channel_id":          str(channel.id),
            "message_id":          str(msg.id),
            "end_time":            end_time,
            "started_by":          str(interaction.user.id),
            "selected_discord_id": str(selected_user.id),
            "finished":            False,
            "created_at":          _now(),
        }
        result = await self.bot.db.giveaways.insert_one(doc)
        doc["_id"] = result.inserted_id
        gid = str(result.inserted_id)

        task = asyncio.create_task(_run_giveaway(self.bot, doc))
        self._giveaway_tasks[gid] = task

        await interaction.followup.send(
            embed=discord.Embed(
                title="\u2705 Selected Giveaway Started",
                description=(
                    "Giveaway for **{}** started in <#{}>.\n"
                    "Selected winner: {}\n"
                    "Ends: {}"
                ).format(prize, channel.id, selected_user.mention, _ts(end_time)),
                color=Config.COLOR_SUCCESS,
            ),
            ephemeral=True,
        )
        logger.info(
            "Selected giveaway by %s: prize='%s' winner=%s",
            interaction.user, prize, selected_user,
        )

    # ── /bound ────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="bound",
        description="Show how many accounts are bound",
    )
    async def bound(self, interaction: discord.Interaction) -> None:
        count = await self.bot.db.bound_accounts.count_documents({})
        await interaction.response.send_message(
            embed=discord.Embed(
                title="\U0001f517 Bound Accounts",
                description=(
                    "**{:,}** Discord account(s) have bound their WWM account."
                ).format(count),
                color=Config.COLOR_INFO,
            )
        )

    # ── /boundinfo ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="boundinfo",
        description="Show binding info for a user",
    )
    @app_commands.describe(user="The Discord member to look up")
    @is_admin()
    async def boundinfo(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        doc = await self.bot.db.bound_accounts.find_one(
            {"discord_id": str(user.id)}
        )
        if not doc:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="\u274c **{}** has not bound a game account.".format(
                        user.display_name
                    ),
                    color=Config.COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="\U0001f517 Bound Account Info",
            color=Config.COLOR_INFO,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="\U0001f464 Discord User", value=user.mention,                      inline=True)
        embed.add_field(name="\U0001f3ae WWM UID",      value="`{}`".format(doc["uid"]),          inline=True)
        embed.add_field(name="\U0001f55b Bound At",     value=_ts(doc["bound_at"]),               inline=True)
        embed.add_field(name="\U0001f4db Username",     value=doc.get("discord_username", "?"),  inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /unbind ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="unbind",
        description="[Admin] Remove a user's account binding",
    )
    @app_commands.describe(user="The Discord member to unbind")
    @is_admin()
    async def unbind(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        doc = await self.bot.db.bound_accounts.find_one(
            {"discord_id": str(user.id)}
        )
        if not doc:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="\u274c **{}** has no binding to remove.".format(
                        user.display_name
                    ),
                    color=Config.COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return

        uid = doc["uid"]
        await self.bot.db.bound_accounts.delete_one({"discord_id": str(user.id)})
        logger.info(
            "Account unbound: %s (%s) UID=%s by %s",
            user, user.id, uid, interaction.user,
        )

        await interaction.response.send_message(
            embed=discord.Embed(
                title="\u2705 Account Unbound",
                description=(
                    "**{}**'s binding has been removed.\n"
                    "**WWM UID:** `{}`\n"
                    "They can now bind again."
                ).format(user.display_name, uid),
                color=Config.COLOR_SUCCESS,
            ),
            ephemeral=True,
        )

        # Log removal
        log_embed = discord.Embed(
            title="\U0001f517 Account Unbound",
            color=Config.COLOR_WARNING,
            timestamp=_now(),
        )
        log_embed.add_field(name="\U0001f464 User",       value=user.mention,              inline=True)
        log_embed.add_field(name="\U0001f3ae WWM UID",    value="`{}`".format(uid),         inline=True)
        log_embed.add_field(name="\U0001f6e1\ufe0f By",   value=interaction.user.mention,  inline=True)
        log_embed.set_footer(text="WWM Account Binding System")
        await _send_log(self.bot, log_embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Binding(bot))
