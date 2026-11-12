# -*- coding: utf-8 -*-
"""
WWM - Guild Join Application System
Integrates with existing bot. Uses existing bot.db, Config, logger, permissions.
No new DB connections. No modification to other files.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import IndexModel, ASCENDING

from config import Config
from utils.permissions import is_admin

logger = logging.getLogger("wwm.guild_join")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    return "<t:{}:F>".format(int(dt.timestamp()))


def _is_mod(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    mod_roles = getattr(Config, "GUILD_MOD_ROLES", [])
    return any(r.id in mod_roles for r in member.roles)


def _build_review_embed(
    user:         discord.abc.User,
    uid:          str,
    char_name:    str,
    submitted_at: datetime,
    status:       str,
    moderator:    Optional[discord.Member] = None,
    reviewed_at:  Optional[datetime] = None,
) -> discord.Embed:
    status_map = {
        "Pending":  ("\U0001f7e1 Pending",  Config.COLOR_WARNING),
        "Approved": ("\U0001f7e2 Approved", Config.COLOR_SUCCESS),
        "Rejected": ("\U0001f534 Rejected", Config.COLOR_ERROR),
    }
    status_str, color = status_map.get(status, ("\u2753 Unknown", Config.COLOR_DARK))

    embed = discord.Embed(
        title="\U0001f4e5 Guild Join Application",
        color=color,
        timestamp=submitted_at,
    )
    embed.add_field(name="\U0001f464 Applicant",   value=user.mention,              inline=True)
    embed.add_field(name="\U0001f4db Username",    value=str(user.name),            inline=True)
    embed.add_field(name="\U0001f194 Discord ID",  value="`{}`".format(user.id),    inline=True)
    embed.add_field(name="\U0001f3ae WWM UID",     value="`{}`".format(uid),        inline=True)
    embed.add_field(name="\u2694\ufe0f Character", value=char_name,                 inline=True)
    embed.add_field(name="\U0001f4ca Status",      value=status_str,                inline=True)
    embed.add_field(name="\U0001f55b Submitted",   value=_ts(submitted_at),         inline=False)

    if moderator and reviewed_at:
        action = "Approved" if status == "Approved" else "Rejected"
        embed.add_field(name="\U0001f6e1\ufe0f {} By".format(action),
                        value=moderator.mention, inline=True)
        embed.add_field(name="\U0001f55b {} Time".format(action),
                        value=_ts(reviewed_at), inline=True)

    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="Where Winds Meet \u2022 Guild Application")
    return embed


# ── Application Modal ─────────────────────────────────────────────────────────

class ApplicationModal(discord.ui.Modal, title="Guild Join Application"):
    wwm_uid = discord.ui.TextInput(
        label="WWM UID",
        placeholder="Enter your in-game UID (numbers only)",
        required=True,
        max_length=20,
    )
    character_name = discord.ui.TextInput(
        label="Character Name",
        placeholder="Enter your in-game character name",
        required=True,
        max_length=30,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        uid  = self.wwm_uid.value.strip()
        name = self.character_name.value.strip()
        user = interaction.user
        db   = self.bot.db

        # Validate: digits only
        if not uid.isdigit():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="\u274c Invalid UID",
                    description="WWM UID must contain **numbers only**. Please check and try again.",
                    color=Config.COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        # Validate: no existing Pending/Approved application
        existing_user = await db.guild_applications.find_one({
            "discord_id": str(user.id),
            "status":     {"$in": ["Pending", "Approved"]},
        })
        if existing_user:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="\u274c Application Already Exists",
                    description=(
                        "You already have a **{}** application.\n"
                        "You cannot submit another while a Pending or "
                        "Approved application exists."
                    ).format(existing_user["status"]),
                    color=Config.COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return

        # Validate: UID not already taken
        existing_uid = await db.guild_applications.find_one({"wwm_uid": uid})
        if existing_uid:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="\u274c UID Already Registered",
                    description=(
                        "WWM UID `{}` is already linked to another account.\n"
                        "If this is a mistake, contact a moderator."
                    ).format(uid),
                    color=Config.COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        # Save to DB
        submitted_at = _now()
        doc = {
            "discord_id":     str(user.id),
            "username":       user.name,
            "wwm_uid":        uid,
            "character_name": name,
            "status":         "Pending",
            "submitted_at":   submitted_at,
            "moderator_id":   None,
            "reviewed_at":    None,
            "request_msg_id": None,
            "request_ch_id":  None,
        }
        result = await db.guild_applications.insert_one(doc)
        app_id = str(result.inserted_id)
        logger.info("New guild application: %s (%s) UID=%s", user, user.id, uid)

        # Post review embed
        request_ch_id = getattr(Config, "GUILD_REQUEST_CHANNEL", 0)
        request_channel = None
        if request_ch_id:
            request_channel = interaction.guild.get_channel(request_ch_id)
            if not request_channel:
                try:
                    request_channel = await self.bot.fetch_channel(request_ch_id)
                except Exception as e:
                    logger.error("Cannot fetch request channel %s: %s", request_ch_id, e)

        if request_channel:
            review_embed = _build_review_embed(user, uid, name, submitted_at, "Pending")
            view = ReviewView(self.bot, app_id, str(user.id))
            msg  = await request_channel.send(embed=review_embed, view=view)
            await db.guild_applications.update_one(
                {"_id": result.inserted_id},
                {"$set": {
                    "request_msg_id": str(msg.id),
                    "request_ch_id":  str(request_ch_id),
                }},
            )
        else:
            logger.error(
                "Request channel not configured or not found (GUILD_REQUEST_CHANNEL=%s)",
                request_ch_id,
            )

        await interaction.followup.send(
            embed=discord.Embed(
                title="\u2705 Application Submitted",
                description=(
                    "Your application is now **Pending** review.\n\n"
                    "**WWM UID:** `{}`\n"
                    "**Character:** `{}`\n\n"
                    "You will receive a DM when a decision is made."
                ).format(uid, name),
                color=Config.COLOR_SUCCESS,
            ),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error("ApplicationModal error: %s", error)
        try:
            await interaction.followup.send(
                "An unexpected error occurred. Please try again later.",
                ephemeral=True,
            )
        except Exception:
            pass


# ── Persistent Join Panel View ────────────────────────────────────────────────

class JoinPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Join Guild",
        style=discord.ButtonStyle.primary,
        emoji="\U0001f4e5",
        custom_id="wwm_guild_join:apply",
    )
    async def join_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(ApplicationModal(self.bot))


# ── Review View (Approve / Reject) ───────────────────────────────────────────

class ReviewView(discord.ui.View):
    def __init__(self, bot: commands.Bot, app_id: str, applicant_discord_id: str):
        super().__init__(timeout=None)
        self.bot                  = bot
        self.app_id               = app_id
        self.applicant_discord_id = applicant_discord_id

    async def _get_app(self):
        from bson import ObjectId
        return await self.bot.db.guild_applications.find_one(
            {"_id": ObjectId(self.app_id)}
        )

    async def _update_app(self, status: str, mod_id: str, reviewed_at: datetime):
        from bson import ObjectId
        await self.bot.db.guild_applications.update_one(
            {"_id": ObjectId(self.app_id)},
            {"$set": {
                "status":       status,
                "moderator_id": mod_id,
                "reviewed_at":  reviewed_at,
            }},
        )

    async def _check_mod(self, interaction: discord.Interaction) -> bool:
        if not _is_mod(interaction.user):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="\u274c Access Denied",
                    description="Only **Moderators** can approve or reject applications.",
                    color=Config.COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return False
        return True

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        emoji="\u2705",
        custom_id="wwm_guild_join:approve",
    )
    async def approve_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await self._check_mod(interaction):
            return
        await interaction.response.defer()

        app = await self._get_app()
        if not app:
            await interaction.followup.send("Application not found.", ephemeral=True)
            return
        if app["status"] != "Pending":
            await interaction.followup.send(
                embed=discord.Embed(
                    title="\u26a0\ufe0f Already Processed",
                    description="This application is already **{}**.".format(app["status"]),
                    color=Config.COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return

        reviewed_at = _now()
        mod         = interaction.user
        await self._update_app("Approved", str(mod.id), reviewed_at)

        # Assign Guild Member role
        role_assigned = False
        guild_role_id = getattr(Config, "GUILD_MEMBER_ROLE", 0)
        applicant_member = interaction.guild.get_member(int(self.applicant_discord_id))
        if applicant_member and guild_role_id:
            role = interaction.guild.get_role(guild_role_id)
            if role:
                try:
                    await applicant_member.add_roles(
                        role, reason="Guild application approved by {}".format(mod)
                    )
                    role_assigned = True
                except discord.Forbidden:
                    logger.warning("Cannot assign guild member role — missing permission.")

        # Edit embed + disable buttons
        self._disable_all()
        applicant_user = applicant_member or await self.bot.fetch_user(
            int(self.applicant_discord_id)
        )
        updated_embed = _build_review_embed(
            applicant_user, app["wwm_uid"], app["character_name"],
            app["submitted_at"], "Approved", mod, reviewed_at,
        )
        await interaction.message.edit(embed=updated_embed, view=self)

        # DM applicant
        try:
            await applicant_user.send(
                embed=discord.Embed(
                    title="\U0001f7e2 Guild Application Approved!",
                    description=(
                        "Congratulations! Your application to join **Where Winds Meet** "
                        "guild has been **Approved**.\n\n"
                        "**WWM UID:** `{}`\n"
                        "**Character:** `{}`\n\n"
                        "Welcome to the guild! \U0001f389"
                    ).format(app["wwm_uid"], app["character_name"]),
                    color=Config.COLOR_SUCCESS,
                )
            )
        except discord.Forbidden:
            logger.warning("Cannot DM applicant %s.", self.applicant_discord_id)

        await interaction.followup.send(
            embed=discord.Embed(
                title="\u2705 Approved",
                description=(
                    "**{}** has been approved.\n"
                    "{}"
                ).format(
                    applicant_user.name,
                    "Guild Member role assigned." if role_assigned
                    else "\u26a0\ufe0f Role not assigned — check bot permissions.",
                ),
                color=Config.COLOR_SUCCESS,
            ),
            ephemeral=True,
        )
        logger.info(
            "Application APPROVED: discord=%s uid=%s by %s",
            self.applicant_discord_id, app["wwm_uid"], mod,
        )

    @discord.ui.button(
        label="Reject",
        style=discord.ButtonStyle.danger,
        emoji="\u274c",
        custom_id="wwm_guild_join:reject",
    )
    async def reject_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await self._check_mod(interaction):
            return
        await interaction.response.defer()

        app = await self._get_app()
        if not app:
            await interaction.followup.send("Application not found.", ephemeral=True)
            return
        if app["status"] != "Pending":
            await interaction.followup.send(
                embed=discord.Embed(
                    title="\u26a0\ufe0f Already Processed",
                    description="This application is already **{}**.".format(app["status"]),
                    color=Config.COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return

        reviewed_at = _now()
        mod         = interaction.user
        await self._update_app("Rejected", str(mod.id), reviewed_at)

        self._disable_all()
        try:
            applicant_user = (
                interaction.guild.get_member(int(self.applicant_discord_id))
                or await self.bot.fetch_user(int(self.applicant_discord_id))
            )
        except Exception:
            applicant_user = None

        if applicant_user:
            updated_embed = _build_review_embed(
                applicant_user, app["wwm_uid"], app["character_name"],
                app["submitted_at"], "Rejected", mod, reviewed_at,
            )
            await interaction.message.edit(embed=updated_embed, view=self)
            try:
                await applicant_user.send(
                    embed=discord.Embed(
                        title="\U0001f534 Guild Application Rejected",
                        description=(
                            "Unfortunately, your application to join **Where Winds Meet** "
                            "guild has been **Rejected**.\n\n"
                            "**WWM UID:** `{}`\n"
                            "**Character:** `{}`\n\n"
                            "If you believe this is a mistake, please contact a moderator."
                        ).format(app["wwm_uid"], app["character_name"]),
                        color=Config.COLOR_ERROR,
                    )
                )
            except discord.Forbidden:
                logger.warning("Cannot DM applicant %s.", self.applicant_discord_id)

        name = applicant_user.name if applicant_user else self.applicant_discord_id
        await interaction.followup.send(
            embed=discord.Embed(
                title="\U0001f534 Rejected",
                description="**{}**'s application has been rejected.".format(name),
                color=Config.COLOR_ERROR,
            ),
            ephemeral=True,
        )
        logger.info(
            "Application REJECTED: discord=%s uid=%s by %s",
            self.applicant_discord_id, app["wwm_uid"], mod,
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class GuildJoin(commands.Cog):
    """Guild application system — panel, modal, approve/reject."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Re-register persistent views so buttons survive restarts
        self.bot.add_view(JoinPanelView(self.bot))

        # Create MongoDB indexes
        try:
            await self.bot.db.guild_applications.create_indexes([
                IndexModel([("discord_id", ASCENDING)],            name="gj_discord_id"),
                IndexModel([("wwm_uid",    ASCENDING)], unique=True, name="gj_uid_unique"),
                IndexModel([("status",     ASCENDING)],            name="gj_status"),
            ])
        except Exception as e:
            logger.warning("Guild application index creation: %s", e)

        # Re-register pending review views
        try:
            async for app in self.bot.db.guild_applications.find({"status": "Pending"}):
                self.bot.add_view(
                    ReviewView(self.bot, str(app["_id"]), app["discord_id"])
                )
            logger.info("Guild join: persistent views re-registered.")
        except Exception as e:
            logger.error("Failed to re-register review views: %s", e)

    # ── /guild_panel ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="guild_panel",
        description="Post the permanent Guild Join panel in the configured channel",
    )
    @is_admin()
    async def guild_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        panel_ch_id = getattr(Config, "GUILD_JOIN_PANEL_CHANNEL", 0)
        if not panel_ch_id:
            await interaction.followup.send(
                "**GUILD_JOIN_PANEL_CHANNEL** is not set. Add it to Railway Variables.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(panel_ch_id)
        if not channel:
            await interaction.followup.send(
                "Channel `{}` not found. Check **GUILD_JOIN_PANEL_CHANNEL**.".format(panel_ch_id),
                ephemeral=True,
            )
            return

        game_guild_id = getattr(Config, "GAME_GUILD_ID", "YOUR_GUILD_ID")

        embed = discord.Embed(
            title="\U0001f3ef Guild Join Request",
            description=(
                "\u0997\u09bf\u09b2\u09cd\u09a1\u09c7 \u09af\u09cb\u0997 \u09a6\u09bf\u09a4\u09c7 "
                "\u09a8\u09bf\u099a\u09c7\u09b0 \u09a7\u09be\u09aa\u0997\u09c1\u09b2\u09cb "
                "\u0985\u09a8\u09c1\u09b8\u09b0\u09a3 \u0995\u09b0\u09c1\u09a8\u0964\n\n"
                "\u2460 \u09aa\u09cd\u09b0\u09a5\u09ae\u09c7 **Where Winds Meet** \u0997\u09c7\u09ae\u09c7 "
                "\u09a8\u09bf\u099a\u09c7\u09b0 Guild ID-\u09a4\u09c7 Join Request \u09aa\u09be\u09a0\u09be\u09a8:\n\n"
                "```\n{}\n```\n"
                "\u2461 \u0997\u09c7\u09ae\u09c7 Join Request \u09aa\u09be\u09a0\u09be\u09a8\u09cb\u09b0 "
                "\u09aa\u09b0 \u09a8\u09bf\u099a\u09c7\u09b0 **\"\U0001f4e5 Join Guild\"** "
                "\u09ac\u09be\u099f\u09a8\u09c7 \u0995\u09cd\u09b2\u09bf\u0995 \u0995\u09b0\u09c1\u09a8 "
                "\u098f\u09ac\u0982 \u0986\u09aa\u09a8\u09be\u09b0 **WWM UID** \u0993 "
                "**Character Name** \u09b8\u09be\u09ac\u09ae\u09bf\u099f \u0995\u09b0\u09c1\u09a8\u0964\n\n"
                "\u26a0\ufe0f Join Request \u09aa\u09be\u09a0\u09be\u09a8\u09cb \u099b\u09be\u09dc\u09be "
                "\u0995\u09b0\u09be \u0986\u09ac\u09c7\u09a6\u09a8 \u0997\u09cd\u09b0\u09b9\u09a3 "
                "\u0995\u09b0\u09be \u09b9\u09ac\u09c7 \u09a8\u09be\u0964"
            ).format(game_guild_id),
            color=Config.COLOR_PRIMARY,
        )
        embed.set_footer(text="Where Winds Meet \u2022 Guild Application System")

        view = JoinPanelView(self.bot)
        await channel.send(embed=embed, view=view)

        await interaction.followup.send(
            embed=discord.Embed(
                title="\u2705 Panel Posted",
                description="Guild join panel posted in <#{}>.".format(panel_ch_id),
                color=Config.COLOR_SUCCESS,
            ),
            ephemeral=True,
        )

    # ── /guild_applications ───────────────────────────────────────────────────

    @app_commands.command(
        name="guild_applications",
        description="View guild applications filtered by status",
    )
    @app_commands.describe(status="Filter: Pending / Approved / Rejected")
    @app_commands.choices(status=[
        app_commands.Choice(name="Pending",  value="Pending"),
        app_commands.Choice(name="Approved", value="Approved"),
        app_commands.Choice(name="Rejected", value="Rejected"),
    ])
    @is_admin()
    async def guild_applications(
        self,
        interaction: discord.Interaction,
        status: str = "Pending",
    ):
        await interaction.response.defer(ephemeral=True)

        apps = await self.bot.db.guild_applications.find(
            {"status": status}
        ).sort("submitted_at", -1).limit(10).to_list(10)

        if not apps:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="No {} Applications".format(status),
                    description="No **{}** applications found.".format(status),
                    color=Config.COLOR_INFO,
                ),
                ephemeral=True,
            )
            return

        icons = {"Pending": "\U0001f7e1", "Approved": "\U0001f7e2", "Rejected": "\U0001f534"}
        embed = discord.Embed(
            title="{} {} Applications".format(icons.get(status, ""), status),
            description="Showing last {} result(s).".format(len(apps)),
            color=Config.COLOR_INFO,
        )
        for app in apps:
            submitted = app.get("submitted_at")
            embed.add_field(
                name="{} — UID: {}".format(app.get("username", "?"), app.get("wwm_uid", "?")),
                value=(
                    "Discord: `{}`\nCharacter: `{}`\nSubmitted: {}"
                ).format(
                    app.get("discord_id"),
                    app.get("character_name"),
                    _ts(submitted) if submitted else "Unknown",
                ),
                inline=False,
            )
        total = await self.bot.db.guild_applications.count_documents({"status": status})
        embed.set_footer(text="Total {}: {}".format(status, total))
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /guild_app_reset ──────────────────────────────────────────────────────

    @app_commands.command(
        name="guild_app_reset",
        description="Delete a user's application so they can reapply",
    )
    @app_commands.describe(member="The member whose application to reset")
    @is_admin()
    async def guild_app_reset(
        self, interaction: discord.Interaction, member: discord.Member
    ):
        result = await self.bot.db.guild_applications.delete_many(
            {"discord_id": str(member.id)}
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="\u2705 Application Reset",
                description=(
                    "Deleted **{}** application(s) for {}.\n"
                    "They can now submit a new application."
                ).format(result.deleted_count, member.mention),
                color=Config.COLOR_SUCCESS,
            ),
            ephemeral=True,
        )
        logger.info(
            "Application reset for %s (%s) by %s — %d deleted",
            member, member.id, interaction.user, result.deleted_count,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildJoin(bot))
