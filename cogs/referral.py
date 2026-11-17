# -*- coding: utf-8 -*-
"""
WWM - Referral Rewards, Points Wallet & Lucky Draw (Cog)
=========================================================
Independent from XP (cogs/leveling.py) and from the account-binding giveaway
(cogs/binding.py: /giveaway, /sgiveaway). Separate collections, separate commands.

Members : /myinvite  /mypoints  /luckydraw  /myrewards  /pointsboard
          + the persistent panel (My Points, Lucky Draw Now, My Invite Link,
            My Rewards, Leaderboard)
Admins  : /referral setup|enable|disable|stats|review|config
          /prize add|edit|remove|inventory|winners|deliver
          /points add|remove|check
          /draw create|start|close|results|cancel

Note: the XP leaderboard already owns /leaderboard, so the points leaderboard is
/pointsboard (a second /leaderboard would be a duplicate command name).

Business logic lives in utils/ (points_service, referral_service, invite_tracker,
lucky_draw_service). This file only talks to Discord.
"""

import asyncio
import logging
from datetime import timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from utils.invite_tracker import InviteTracker
from utils.lucky_draw_service import DEFAULT_CAMPAIGN, LuckyDrawService
from utils.permissions import _is_owner, create_error_embed, create_success_embed
from utils.points_service import PointsService
from utils.referral_service import ReferralService

logger = logging.getLogger("wwm.referral")

FOOTER = "Where Winds Meet \u2022 Rewards"
MAINTENANCE_SECONDS = 300
PANEL_REFRESH_DELAY = 20

NO_WIN_TEXT = ("Oops! Looks like you are not selected this time. \U0001f622\n"
               "Better luck next time!")


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pts(n: int) -> str:
    return "{} point{}".format(n, "" if n == 1 else "s")


def _when(dt) -> str:
    if dt is None:
        return "-"
    return discord.utils.format_dt(dt.replace(tzinfo=timezone.utc), "R")


def _embed(title: str, description: str = "", color: int = None) -> discord.Embed:
    e = discord.Embed(title=title, description=description,
                      color=color if color is not None else Config.COLOR_PRIMARY)
    e.set_footer(text=FOOTER)
    return e


def staff_only():
    """Administrator, bot owner, or a role listed in REFERRAL_ADMIN_ROLE_IDS / /referral config."""
    async def predicate(interaction: discord.Interaction) -> bool:
        user = interaction.user
        if _is_owner(user.id):
            return True
        perms = getattr(user, "guild_permissions", None)
        if perms is not None and perms.administrator:
            return True
        cog = interaction.client.get_cog("Referral")
        if cog is not None and interaction.guild is not None:
            cfg = await cog.referral.get_config(str(interaction.guild.id))
            if cfg.admin_role_ids & {r.id for r in getattr(user, "roles", [])}:
                return True
        await interaction.response.send_message(
            embed=create_error_embed("Access Denied", "You are not allowed to use this command."),
            ephemeral=True)
        return False
    return app_commands.check(predicate)


# ─────────────────────────────────────────────────────────────────────────────
# Persistent panel
# ─────────────────────────────────────────────────────────────────────────────

class ReferralPanelView(discord.ui.View):
    """Persistent view: custom_ids are fixed so the buttons survive restarts."""

    def __init__(self, cog: "Referral"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="My Points", emoji="\U0001f48e", style=discord.ButtonStyle.primary,
                       custom_id="wwmref:points", row=0)
    async def my_points(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.guarded(interaction, self.cog.show_points)

    @discord.ui.button(label="Lucky Draw Now", emoji="\U0001f381", style=discord.ButtonStyle.success,
                       custom_id="wwmref:draw", row=0)
    async def lucky_draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.guarded(interaction, self.cog.do_lucky_draw, 1, None)

    @discord.ui.button(label="My Invite Link", emoji="\U0001f517", style=discord.ButtonStyle.primary,
                       custom_id="wwmref:invite", row=0)
    async def my_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.guarded(interaction, self.cog.show_invite)

    @discord.ui.button(label="My Rewards", emoji="\U0001f3c6", style=discord.ButtonStyle.secondary,
                       custom_id="wwmref:rewards", row=1)
    async def my_rewards(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.guarded(interaction, self.cog.show_rewards)

    @discord.ui.button(label="Leaderboard", emoji="\U0001f4ca", style=discord.ButtonStyle.secondary,
                       custom_id="wwmref:board", row=1)
    async def board(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.guarded(interaction, self.cog.show_board)


class ClaimView(discord.ui.View):
    """Ephemeral helper shown under My Rewards: mark pending prizes as claimed."""

    def __init__(self, cog: "Referral", guild_id: str, user_id: str):
        super().__init__(timeout=120)
        self.cog, self.guild_id, self.user_id = cog, guild_id, user_id

    @discord.ui.button(label="Claim my pending prizes", emoji="\U0001f4e6", style=discord.ButtonStyle.success)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This button is not for you.", ephemeral=True)
            return
        count = await self.cog.draws.claim_pending(self.guild_id, self.user_id)
        button.disabled = True
        text = ("\u2705 Marked **{}** prize(s) as claimed. A staff member will deliver them soon."
                .format(count) if count else "You have no pending prizes.")
        await interaction.response.edit_message(content=text, view=self)


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────

class Referral(commands.Cog):
    """Invite rewards, points wallet and lucky draw."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.points: Optional[PointsService] = None
        self.referral: Optional[ReferralService] = None
        self.draws: Optional[LuckyDrawService] = None
        self.tracker: Optional[InviteTracker] = None
        self._tasks = []
        self._panel_refresh = {}

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        db = self.bot.db
        if not getattr(db, "rewards_ready", False):
            raise RuntimeError("Reward migrations did not complete - referral system not loaded "
                               "(existing features are unaffected). Check the startup log.")
        for problem in Config.validate_referral():
            logger.warning("Config: %s", problem)

        self.points = PointsService(db)
        self.referral = ReferralService(db, self.points)
        self.draws = LuckyDrawService(db, self.points)
        self.tracker = InviteTracker(db)

        self.bot.add_view(ReferralPanelView(self))
        self._tasks.append(asyncio.create_task(self._startup()))
        logger.info("Referral system loaded (MongoDB transactions available: %s; "
                    "the ledger does not depend on them).", getattr(db, "supports_transactions", False))

    def cog_unload(self) -> None:
        for task in self._tasks + list(self._panel_refresh.values()):
            task.cancel()

    async def _startup(self) -> None:
        try:
            await self.bot.wait_until_ready()
            await self._maintenance()
            for guild in self.bot.guilds:
                count = await self.tracker.sync_guild(guild)
                logger.info("Invite snapshot for %s: %s invite(s)", guild.name,
                            "unavailable" if count is None else count)
            while True:
                await asyncio.sleep(MAINTENANCE_SECONDS)
                await self._maintenance()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Referral background task crashed")

    async def _maintenance(self) -> None:
        """Finish anything a crash left half-done. Everything here is idempotent."""
        try:
            await self.points.recover_pending()
            await self.referral.reconcile()
            await self.draws.resume_unfinished()
        except Exception:
            logger.exception("Referral maintenance failed")

    # ── shared interaction plumbing ───────────────────────────────────────

    async def guarded(self, interaction: discord.Interaction, handler, *args) -> None:
        """Run a user-facing handler with ephemeral defer + uniform error handling."""
        try:
            if interaction.guild is None:
                await interaction.response.send_message("Use this inside the server.", ephemeral=True)
                return
            if not interaction.response.is_done():
                # thinking=True so button clicks also get a private (ephemeral) reply
                await interaction.response.defer(ephemeral=True, thinking=True)
            cfg = await self.referral.get_config(str(interaction.guild.id))
            if not cfg.enabled:
                await interaction.followup.send(
                    embed=_embed("Rewards are paused", "The reward system is currently disabled.",
                                 Config.COLOR_WARNING), ephemeral=True)
                return
            await handler(interaction, cfg, *args)
        except Exception:
            logger.exception("Reward interaction failed")
            try:
                await interaction.followup.send(
                    embed=create_error_embed("Something went wrong",
                                             "Please try again in a moment. Your points are safe."),
                    ephemeral=True)
            except Exception:
                pass

    async def _reply(self, interaction: discord.Interaction, **kwargs) -> None:
        """Send the (already persisted) result. A failure here can never re-run a draw."""
        try:
            await interaction.followup.send(ephemeral=True, **kwargs)
        except Exception:
            logger.warning("Could not deliver reply for interaction %s (result is saved, "
                           "see /myrewards)", interaction.id)

    # ── panel ─────────────────────────────────────────────────────────────

    async def build_panel_embed(self, guild: discord.Guild, cfg) -> discord.Embed:
        campaign = DEFAULT_CAMPAIGN
        if cfg.default_campaign:
            draw = await self.draws.get_draw(str(guild.id), cfg.default_campaign)
            if draw and draw.get("status") == "open":
                campaign = draw["draw_id"]
        prizes = await self.draws.prize_pool_summary(str(guild.id), campaign)

        embed = discord.Embed(
            title="\U0001f48e Invite Friends & Earn Rewards",
            description=("Got friends who would love our community? Invite them and let's grow together!\n\n"
                         "The more friends you bring, the more chances you earn!"),
            color=Config.COLOR_PRIMARY)

        chat_where = ""
        if cfg.channel_ids:
            chat_where = " in " + " or ".join("<#{}>".format(c) for c in sorted(cfg.channel_ids))
        embed.add_field(name="\U0001f4dd Quick Guide", value=(
            "\u2022 Press **\U0001f517 My Invite Link** to get your personal invite link.\n"
            "\u2022 Share it with friends. You earn points when they join."), inline=False)
        embed.add_field(name="\u2b50 Points Earned", value=(
            "\u2b50 {} per valid referral.\n"
            "\u2b50 {} for your first eligible chat message{} (one-time only).\n"
            "\u2b50 1 point = 1 lucky draw entry.\n"
            "\u2b50 Minimum account age: {} days."
        ).format(_pts(cfg.referral_points), _pts(cfg.chat_points), chat_where, cfg.min_account_age_days),
            inline=False)
        if prizes:
            lines = ["\u2022 **{}x** {}".format(p["stock"], p["name"]) for p in prizes[:12]]
            more = len(prizes) - 12
            if more > 0:
                lines.append("\u2026and {} more".format(more))
            pool = "\n".join(lines)
        else:
            pool = "No prizes are available right now. Check back soon!"
        embed.add_field(name="\U0001f381 Prize Pool", value=pool[:1000], inline=False)
        embed.add_field(name="\u200b", value="Click **Lucky Draw Now** below to play!", inline=False)
        embed.set_footer(text=FOOTER)
        return embed

    async def refresh_panel(self, guild: discord.Guild) -> bool:
        cfg = await self.referral.get_config(str(guild.id))
        if not (cfg.panel_channel_id and cfg.panel_message_id):
            return False
        channel = guild.get_channel(cfg.panel_channel_id)
        if channel is None:
            return False
        try:
            message = await channel.fetch_message(cfg.panel_message_id)
            await message.edit(embed=await self.build_panel_embed(guild, cfg), view=ReferralPanelView(self))
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.warning("Could not refresh reward panel in %s: %s", guild.name, e)
            return False

    def schedule_panel_refresh(self, guild: discord.Guild) -> None:
        """Debounced refresh so busy draws do not hammer the Discord API."""
        existing = self._panel_refresh.get(guild.id)
        if existing and not existing.done():
            return

        async def later():
            await asyncio.sleep(PANEL_REFRESH_DELAY)
            await self.refresh_panel(guild)

        self._panel_refresh[guild.id] = asyncio.create_task(later())

    # ── member-facing handlers ────────────────────────────────────────────

    async def show_points(self, interaction: discord.Interaction, cfg) -> None:
        gid, uid = str(interaction.guild.id), str(interaction.user.id)
        s = await self.points.get_user_stats(gid, uid)
        embed = _embed("\U0001f48e My Points",
                       "**Current balance: {}**\n\nEvery point is one lucky draw entry.".format(_pts(s["points_balance"])))
        embed.add_field(name="Referral points", value=str(s["referral_points"]))
        embed.add_field(name="Chat points", value=str(s["chat_points"]))
        embed.add_field(name="Total earned", value=str(s["total_points_earned"]))
        embed.add_field(name="Total spent", value=str(s["total_points_spent"]))
        embed.add_field(name="Valid referrals", value=str(s["total_valid_referrals"]))
        embed.add_field(name="Draw entries used", value=str(s["total_draw_entries"]))
        embed.add_field(name="How to earn", inline=False, value=(
            "\u2022 {} per friend who joins with your link\n"
            "\u2022 {} for your first message in an eligible channel").format(
                _pts(cfg.referral_points), _pts(cfg.chat_points)))
        await self._reply(interaction, embed=embed)

    async def show_invite(self, interaction: discord.Interaction, cfg) -> None:
        guild, member = interaction.guild, interaction.user
        channel = self._invite_channel(guild, cfg, interaction.channel)
        if channel is None:
            await self._reply(interaction, embed=create_error_embed(
                "No invite channel",
                "I could not find a channel where I may create invites. Ask an admin to set one with "
                "`/referral config invite_channel`."))
            return
        try:
            url = await self.tracker.get_or_create_personal_invite(guild, member, channel)
        except discord.Forbidden:
            await self._reply(interaction, embed=create_error_embed(
                "Missing permission",
                "I need **Create Invite** and **Manage Server** permissions to make your link."))
            return
        summary = await self.referral.user_summary(str(guild.id), str(member.id))
        embed = _embed("\U0001f517 Your Personal Invite Link", url)
        embed.add_field(name="Valid referrals", value=str(summary["valid"]))
        embed.add_field(name="Pending review", value=str(summary["pending"]))
        embed.add_field(name="Referral points earned", value=str(summary["points"]))
        embed.add_field(name="Rules", inline=False, value=(
            "\u2022 {} per valid referral\n"
            "\u2022 The invited Discord account must be at least {} days old\n"
            "\u2022 Bots and self-invites do not count; each account is rewarded once").format(
                _pts(cfg.referral_points), cfg.min_account_age_days))
        await self._reply(interaction, embed=embed)

    @staticmethod
    def _invite_channel(guild, cfg, current):
        me = guild.me
        candidates = []
        if cfg.invite_channel_id:
            candidates.append(guild.get_channel(cfg.invite_channel_id))
        candidates.append(current)
        candidates.append(guild.system_channel)
        candidates.append(guild.rules_channel)
        candidates.extend(guild.text_channels)
        for ch in candidates:
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(me).create_instant_invite:
                return ch
        return None

    async def do_lucky_draw(self, interaction: discord.Interaction, cfg, entries: int = 1,
                            campaign: Optional[str] = None) -> None:
        gid, uid = str(interaction.guild.id), str(interaction.user.id)
        ref = campaign or cfg.default_campaign or DEFAULT_CAMPAIGN
        target = await self.draws.resolve_campaign(gid, ref)
        if target is None:
            await self._reply(interaction, embed=create_error_embed(
                "Campaign not found", "That campaign does not exist."))
            return

        request_id = str(interaction.id)           # unique per click -> replays return the saved result
        if target.get("type") == "raffle":
            out = await self.draws.enter_raffle(gid, uid, target["draw_id"], request_id, entries,
                                                max_entries=cfg.max_draw_entries * 10)
        else:
            out = await self.draws.instant_draw(gid, uid, request_id, entries, target["draw_id"],
                                                max_entries=cfg.max_draw_entries)

        if not out.ok:
            await self._reply(interaction, embed=self._draw_error_embed(out, cfg))
            return

        if target.get("type") == "raffle":
            embed = _embed("\U0001f39f\ufe0f Raffle entry confirmed",
                           "You entered **{}** with **{}** ticket(s).\nResults are announced when the "
                           "campaign closes.".format(target.get("name"), out.entries),
                           Config.COLOR_SUCCESS)
        else:
            embed = self._instant_result_embed(out)
            self.schedule_panel_refresh(interaction.guild)
        if out.replayed:
            embed.add_field(name="\u200b", value="*This is the original result of your request.*", inline=False)
        embed.add_field(name="Points left", value=str(out.balance_after if out.balance_after is not None else 0))
        await self._reply(interaction, embed=embed)

    def _instant_result_embed(self, out) -> discord.Embed:
        wins = [r for r in out.results if not r.get("is_losing")]
        misses = len(out.results) - len(wins)
        if not wins:
            embed = _embed("\U0001f381 Lucky Draw", NO_WIN_TEXT, Config.COLOR_WARNING)
        else:
            lines = ["\U0001f389 **{}**".format(r["name"]) for r in wins]
            if misses:
                lines.append("\u2026and {} draw(s) without a prize.".format(misses))
            embed = _embed("\U0001f389 Congratulations!",
                           "You won:\n" + "\n".join(lines) +
                           "\n\nOpen **\U0001f3c6 My Rewards** to claim your prize.",
                           Config.COLOR_SUCCESS)
        if out.refunded:
            embed.add_field(name="Refund", value="{} returned (prize pool ran out).".format(_pts(out.refunded)),
                            inline=False)
        return embed

    @staticmethod
    def _draw_error_embed(out, cfg) -> discord.Embed:
        messages = {
            "insufficient_funds": "You do not have enough points. Invite friends or chat to earn more!",
            "no_prizes": "There are no prizes available right now. Please check back later.",
            "campaign_closed": "This lucky draw is not open.",
            "invalid_entries": "You can use between 1 and {} entries per draw.".format(cfg.max_draw_entries),
            "cap_reached": "You reached the entry limit for this campaign.",
        }
        text = messages.get(out.error, "The draw could not be completed. No points were lost.")
        if out.error == "insufficient_funds" and out.balance_after is not None:
            text += "\nYour balance: **{}**.".format(_pts(out.balance_after))
        return create_error_embed("Lucky Draw", text)

    async def show_rewards(self, interaction: discord.Interaction, cfg) -> None:
        gid, uid = str(interaction.guild.id), str(interaction.user.id)
        wins = await self.draws.get_wins(gid, uid)
        history = await self.draws.get_history(gid, uid)
        icons = {"pending": "\u23f3 Pending", "claimed": "\U0001f4e6 Claimed", "delivered": "\u2705 Delivered",
                 "cancelled": "\u274c Cancelled"}
        embed = _embed("\U0001f3c6 My Rewards")
        if wins:
            embed.add_field(name="Prizes won", inline=False, value="\n".join(
                "`{}` **{}** - {} ({})".format(w["winner_id"], w["prize_name"],
                                              icons.get(w["status"], w["status"]), _when(w["created_at"]))
                for w in wins)[:1000])
        else:
            embed.add_field(name="Prizes won", value="Nothing yet - try the **Lucky Draw**!", inline=False)
        if history:
            rows = []
            for h in history:
                if h["mode"] == "instant":
                    won = [r["name"] for r in h.get("results", []) if not r.get("is_losing")]
                    summary = ", ".join(won) if won else "no prize"
                    rows.append("\U0001f381 x{} - {} ({})".format(h["entries"], summary, _when(h["created_at"])))
                else:
                    state = "refunded" if h["status"] == "refunded" else "entered"
                    rows.append("\U0001f39f\ufe0f {} - {} x{} ({})".format(
                        h.get("campaign_name", "Raffle"), state, h["entries"], _when(h["created_at"])))
            embed.add_field(name="Draw history", value="\n".join(rows)[:1000], inline=False)
        pending = any(w["status"] == "pending" for w in wins)
        view = ClaimView(self, gid, uid) if pending else None
        kwargs = {"embed": embed}
        if view is not None:
            kwargs["view"] = view
        await self._reply(interaction, **kwargs)

    async def show_board(self, interaction: discord.Interaction, cfg) -> None:
        rows = await self.points.get_leaderboard(str(interaction.guild.id), 10)
        if not rows:
            await self._reply(interaction, embed=_embed("\U0001f4ca Points Leaderboard",
                                                        "Nobody has earned points yet - be the first!"))
            return
        medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
        lines = []
        for i, r in enumerate(rows):
            lead = medals[i] if i < 3 else "`#{}`".format(i + 1)
            lines.append("{} <@{}> - **{}** earned \u2022 {} referral(s)".format(
                lead, r["user_id"], r.get("total_points_earned", 0), r.get("total_valid_referrals", 0)))
        await self._reply(interaction, embed=_embed("\U0001f4ca Points Leaderboard", "\n".join(lines)))

    # ── member slash commands ─────────────────────────────────────────────

    @app_commands.command(name="myinvite", description="Get your personal invite link and referral stats")
    @app_commands.guild_only()
    async def myinvite(self, interaction: discord.Interaction):
        await self.guarded(interaction, self.show_invite)

    @app_commands.command(name="mypoints", description="Check your reward points")
    @app_commands.guild_only()
    async def mypoints(self, interaction: discord.Interaction):
        await self.guarded(interaction, self.show_points)

    @app_commands.command(name="luckydraw", description="Spend points on the lucky draw (1 point = 1 entry)")
    @app_commands.describe(entries="How many entries to use (default 1)",
                           campaign="Campaign to join (default: the main lucky draw)")
    @app_commands.guild_only()
    async def luckydraw(self, interaction: discord.Interaction,
                        entries: app_commands.Range[int, 1, 100] = 1,
                        campaign: Optional[str] = None):
        await self.guarded(interaction, self.do_lucky_draw, entries, campaign)

    @luckydraw.autocomplete("campaign")
    async def luckydraw_campaign_ac(self, interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        draws = await self.draws.list_draws(str(interaction.guild.id), statuses=["open"])
        choices = [app_commands.Choice(name="Main lucky draw", value=DEFAULT_CAMPAIGN)]
        choices += [app_commands.Choice(name="{} ({})".format(d["name"], d["type"])[:100], value=d["draw_id"])
                    for d in draws if current.lower() in d["name"].lower()]
        return choices[:25]

    @app_commands.command(name="myrewards", description="See your prizes and draw history")
    @app_commands.guild_only()
    async def myrewards(self, interaction: discord.Interaction):
        await self.guarded(interaction, self.show_rewards)

    @app_commands.command(name="pointsboard", description="Top members by reward points earned")
    @app_commands.guild_only()
    async def pointsboard(self, interaction: discord.Interaction):
        await self.guarded(interaction, self.show_board)

    # ─────────────────────────────────────────────────────────────────────
    # Listeners (these ADD to the existing leveling / AutoMod listeners)
    # ─────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (message.author.bot or message.webhook_id is not None or message.guild is None
                or message.type not in (discord.MessageType.default, discord.MessageType.reply)):
            return
        try:
            channel = message.channel
            ids = {channel.id, getattr(channel, "parent_id", None)}
            claimed = await self.referral.claim_chat_reward(
                str(message.guild.id), str(message.author.id), ids)
            if claimed:
                logger.info("Chat reward paid to %s in %s", message.author.id, message.guild.id)
        except Exception:
            logger.exception("Chat reward failed")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        try:
            cfg = await self.referral.get_config(str(member.guild.id))
            if not cfg.enabled:
                return
            attribution = await self.tracker.resolve_join(member.guild, member)
            in_guild = bool(attribution.inviter_id and member.guild.get_member(attribution.inviter_id))
            doc, awarded = await self.referral.process_join(
                str(member.guild.id), member, attribution, inviter_in_guild=in_guild)
            if doc is not None:
                logger.info("Referral %s: invited=%s inviter=%s status=%s", member.guild.id, member.id,
                            doc.get("inviter_id"), doc.get("validation_status"))
            if awarded:
                await self._announce_referral(member, doc, cfg)
        except Exception:
            logger.exception("Referral processing failed for %s", member.id)

    async def _announce_referral(self, member, doc, cfg) -> None:
        if not cfg.announcement_channel_id:
            return
        channel = member.guild.get_channel(cfg.announcement_channel_id)
        if channel is None:
            return
        try:
            await channel.send(
                embed=_embed("\U0001f389 New referral!", "<@{}> invited <@{}> and earned **{}**.".format(
                    doc["inviter_id"], member.id, _pts(doc.get("points_awarded", 0))), Config.COLOR_SUCCESS),
                allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        try:
            await self.tracker.on_invite_created(invite)
        except Exception:
            logger.exception("on_invite_create failed")

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        try:
            await self.tracker.on_invite_deleted(invite)
        except Exception:
            logger.exception("on_invite_delete failed")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        try:
            await self.tracker.sync_guild(guild)
        except Exception:
            logger.exception("Invite sync failed for new guild")

    # ─────────────────────────────────────────────────────────────────────
    # /referral  (admin)
    # ─────────────────────────────────────────────────────────────────────

    referral_group = app_commands.Group(name="referral", description="Referral reward settings (staff)",
                                        guild_only=True)

    @referral_group.command(name="setup", description="Post (or refresh) the Invite & Rewards panel")
    @app_commands.describe(channel="Channel for the panel")
    @staff_only()
    async def referral_setup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        cfg = await self.referral.get_config(str(interaction.guild.id))
        embed = await self.build_panel_embed(interaction.guild, cfg)
        message = None
        if cfg.panel_channel_id == channel.id and cfg.panel_message_id:
            try:
                message = await channel.fetch_message(cfg.panel_message_id)
                await message.edit(embed=embed, view=ReferralPanelView(self))
            except (discord.NotFound, discord.HTTPException):
                message = None
        if message is None:
            try:
                message = await channel.send(embed=embed, view=ReferralPanelView(self))
            except discord.Forbidden:
                await interaction.followup.send(embed=create_error_embed(
                    "Missing permission", "I cannot post in {}.".format(channel.mention)), ephemeral=True)
                return
        await self.referral.update_config(str(interaction.guild.id),
                                          {"panel_channel_id": channel.id, "panel_message_id": message.id})
        await self.referral.audit(str(interaction.guild.id), str(interaction.user.id), "panel_setup",
                                  {"channel_id": channel.id, "message_id": message.id})
        await interaction.followup.send(embed=create_success_embed(
            "Panel ready", "Panel posted in {}.".format(channel.mention)), ephemeral=True)

    @referral_group.command(name="enable", description="Turn the reward system on")
    @staff_only()
    async def referral_enable(self, interaction: discord.Interaction):
        await self.referral.update_config(str(interaction.guild.id), {"enabled": True})
        await self.referral.audit(str(interaction.guild.id), str(interaction.user.id), "enabled", {})
        await interaction.response.send_message(
            embed=create_success_embed("Enabled", "Referral rewards are **on**."), ephemeral=True)

    @referral_group.command(name="disable", description="Turn the reward system off (data is kept)")
    @staff_only()
    async def referral_disable(self, interaction: discord.Interaction):
        await self.referral.update_config(str(interaction.guild.id), {"enabled": False})
        await self.referral.audit(str(interaction.guild.id), str(interaction.user.id), "disabled", {})
        await interaction.response.send_message(
            embed=create_success_embed("Disabled", "Referral rewards are **off**. Balances are kept."),
            ephemeral=True)

    @referral_group.command(name="stats", description="Reward system statistics")
    @staff_only()
    async def referral_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        st = await self.referral.guild_stats(str(interaction.guild.id))
        cfg = await self.referral.get_config(str(interaction.guild.id))
        embed = _embed("\U0001f4c8 Referral Stats")
        embed.add_field(name="Status", value="\u2705 On" if cfg.enabled else "\u26d4 Off")
        embed.add_field(name="Referrals rewarded", value=str(st["rewarded"]))
        embed.add_field(name="Awaiting review", value=str(st["pending_review"]))
        embed.add_field(name="Rejected: account too new", value=str(st["invalid_age"]))
        embed.add_field(name="Rejected: self-invite", value=str(st["invalid_self"]))
        embed.add_field(name="Rejected by staff", value=str(st["rejected"]))
        embed.add_field(name="Chat rewards paid", value=str(st["chat_claims"]))
        embed.add_field(name="Points issued", value=str(st["points_issued"]))
        embed.add_field(name="Points spent", value=str(st["points_spent"]))
        embed.add_field(name="Points in wallets", value=str(st["points_in_circulation"]))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @referral_group.command(name="review", description="Review referrals that could not be verified")
    @app_commands.describe(action="What to do", member="The invited member", inviter="Who invited them (approve)")
    @app_commands.choices(action=[
        app_commands.Choice(name="List pending", value="list"),
        app_commands.Choice(name="Approve", value="approve"),
        app_commands.Choice(name="Reject", value="reject"),
    ])
    @staff_only()
    async def referral_review(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                              member: Optional[discord.Member] = None,
                              inviter: Optional[discord.Member] = None):
        gid = str(interaction.guild.id)
        await interaction.response.defer(ephemeral=True)
        if action.value == "list":
            rows = await self.referral.list_review(gid)
            if not rows:
                await interaction.followup.send(embed=_embed("\U0001f50e Referral Review",
                                                             "Nothing is waiting for review."), ephemeral=True)
                return
            lines = []
            for r in rows:
                cands = ", ".join("<@{}> (`{}`)".format(c["inviter_id"], c["code"])
                                  for c in r.get("candidates", []) if c.get("inviter_id")) or "unknown"
                lines.append("<@{}> - {}\nPossible inviter(s): {}".format(
                    r["invited_user_id"], r.get("validation_reason") or "needs review", cands))
            embed = _embed("\U0001f50e Referral Review", "\n\n".join(lines)[:3900])
            embed.add_field(name="How to decide", inline=False,
                            value="`/referral review action:Approve member:<invited> inviter:<who>`\n"
                                  "`/referral review action:Reject member:<invited>`")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        if member is None:
            await interaction.followup.send(embed=create_error_embed(
                "Member required", "Choose the invited member."), ephemeral=True)
            return
        ok, text = await self.referral.review_decide(
            gid, str(member.id), action.value == "approve",
            str(inviter.id) if inviter else None, str(interaction.user.id))
        embed = create_success_embed("Review", text) if ok else create_error_embed("Review", text)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @referral_group.command(name="config", description="View or change reward settings")
    @app_commands.describe(
        referral_points="Points per valid referral", chat_points="Points for the first chat message",
        min_account_age_days="Minimum Discord account age",
        add_chat_channel="Make a channel eligible for the chat reward",
        remove_chat_channel="Stop a channel from counting",
        announcement_channel="Where referral announcements go",
        invite_channel="Channel used to create personal invites",
        default_campaign="Campaign id used by the panel button (blank = main draw)",
        max_draw_entries="Max entries per instant draw request",
        add_admin_role="Role allowed to use reward admin commands",
        remove_admin_role="Remove such a role")
    @staff_only()
    async def referral_config_cmd(
            self, interaction: discord.Interaction,
            referral_points: Optional[app_commands.Range[int, 0, 1000]] = None,
            chat_points: Optional[app_commands.Range[int, 0, 1000]] = None,
            min_account_age_days: Optional[app_commands.Range[int, 0, 3650]] = None,
            add_chat_channel: Optional[discord.TextChannel] = None,
            remove_chat_channel: Optional[discord.TextChannel] = None,
            announcement_channel: Optional[discord.TextChannel] = None,
            invite_channel: Optional[discord.TextChannel] = None,
            default_campaign: Optional[str] = None,
            max_draw_entries: Optional[app_commands.Range[int, 1, 100]] = None,
            add_admin_role: Optional[discord.Role] = None,
            remove_admin_role: Optional[discord.Role] = None):
        gid = str(interaction.guild.id)
        sets, add_to, remove_from = {}, {}, {}
        for key, value in (("referral_points", referral_points), ("chat_points", chat_points),
                           ("min_account_age_days", min_account_age_days), ("max_draw_entries", max_draw_entries)):
            if value is not None:
                sets[key] = int(value)
        if announcement_channel:
            sets["announcement_channel_id"] = announcement_channel.id
        if invite_channel:
            sets["invite_channel_id"] = invite_channel.id
        if default_campaign is not None:
            sets["default_campaign"] = "" if default_campaign.lower() in ("", "none", "default") else default_campaign.strip()
        if add_chat_channel:
            add_to["channel_ids"] = [add_chat_channel.id]
        if remove_chat_channel:
            remove_from["channel_ids"] = [remove_chat_channel.id]
        if add_admin_role:
            add_to["admin_role_ids"] = [add_admin_role.id]
        if remove_admin_role:
            remove_from["admin_role_ids"] = [remove_admin_role.id]

        if sets or add_to or remove_from:
            await self.referral.update_config(gid, sets, add_to=add_to, remove_from=remove_from)
            await self.referral.audit(gid, str(interaction.user.id), "config_changed",
                                      {"set": sets, "add": add_to, "remove": remove_from})
            self.schedule_panel_refresh(interaction.guild)

        cfg = await self.referral.get_config(gid)
        embed = _embed("\u2699\ufe0f Reward Settings", color=Config.COLOR_INFO)
        embed.add_field(name="Enabled", value=str(cfg.enabled))
        embed.add_field(name="Referral points", value=str(cfg.referral_points))
        embed.add_field(name="Chat points", value=str(cfg.chat_points))
        embed.add_field(name="Min account age", value="{} days".format(cfg.min_account_age_days))
        embed.add_field(name="Max entries / draw", value=str(cfg.max_draw_entries))
        embed.add_field(name="Default campaign", value=cfg.default_campaign or "main draw")
        embed.add_field(name="Chat reward channels", inline=False,
                        value=" ".join("<#{}>".format(c) for c in sorted(cfg.channel_ids)) or "none set")
        embed.add_field(name="Announcements", value="<#{}>".format(cfg.announcement_channel_id)
                        if cfg.announcement_channel_id else "off")
        embed.add_field(name="Invite channel", value="<#{}>".format(cfg.invite_channel_id)
                        if cfg.invite_channel_id else "automatic")
        embed.add_field(name="Extra admin roles", inline=False,
                        value=" ".join("<@&{}>".format(r) for r in sorted(cfg.admin_role_ids)) or "none")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────
    # /prize  (admin)
    # ─────────────────────────────────────────────────────────────────────

    prize_group = app_commands.Group(name="prize", description="Lucky draw prizes (staff)", guild_only=True)

    async def _prize_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        rows = await self.draws.list_prizes(str(interaction.guild.id))
        out = []
        for p in rows:
            label = "{} [{}] stock {}".format(p["name"], p["prize_id"],
                                              "unlimited" if p.get("unlimited") else p.get("stock", 0))
            if current.lower() in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=p["prize_id"]))
        return out[:25]

    async def _draw_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        rows = await self.draws.list_draws(str(interaction.guild.id))
        out = []
        for d in rows:
            label = "{} [{}] {} {}".format(d["name"], d["draw_id"], d["type"], d["status"])
            if current.lower() in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=d["draw_id"]))
        return out[:25]

    @prize_group.command(name="add", description="Add a prize (or a 'no prize' outcome) to a draw pool")
    @app_commands.describe(
        name="Prize name", stock="How many are available", weight="Winning weight (higher = more likely)",
        category="Type of prize, e.g. ticket, item, role", losing="This is a 'no prize' outcome (unlimited)",
        campaign="Campaign id (blank = main lucky draw)", description="Short description")
    @staff_only()
    async def prize_add(self, interaction: discord.Interaction, name: str,
                        stock: app_commands.Range[int, 0, 100000], weight: app_commands.Range[int, 0, 1000000],
                        category: Optional[str] = "general", losing: Optional[bool] = False,
                        campaign: Optional[str] = None, description: Optional[str] = ""):
        gid = str(interaction.guild.id)
        campaign_id = campaign or DEFAULT_CAMPAIGN
        if campaign_id != DEFAULT_CAMPAIGN and await self.draws.get_draw(gid, campaign_id) is None:
            await interaction.response.send_message(embed=create_error_embed(
                "Unknown campaign", "No campaign with id `{}`.".format(campaign_id)), ephemeral=True)
            return
        prize = await self.draws.add_prize(gid, name, stock=stock, weight=weight, category=category or "general",
                                           is_losing=bool(losing), campaign_id=campaign_id,
                                           description=description or "", actor_id=str(interaction.user.id))
        await self.referral.audit(gid, str(interaction.user.id), "prize_added",
                                  {k: prize[k] for k in ("prize_id", "name", "stock", "weight", "is_losing")})
        self.schedule_panel_refresh(interaction.guild)
        await interaction.response.send_message(embed=create_success_embed(
            "Prize added", "`{}` **{}** - stock {}, weight {}".format(
                prize["prize_id"], prize["name"], "unlimited" if prize["unlimited"] else prize["stock"],
                prize["weight"])), ephemeral=True)

    @prize_group.command(name="edit", description="Change a prize")
    @app_commands.describe(prize_id="Prize to edit", name="New name", stock="Set the remaining stock",
                           weight="New winning weight", category="New category",
                           active="Enable / disable the prize", description="New description")
    @staff_only()
    async def prize_edit(self, interaction: discord.Interaction, prize_id: str, name: Optional[str] = None,
                         stock: Optional[app_commands.Range[int, 0, 100000]] = None,
                         weight: Optional[app_commands.Range[int, 0, 1000000]] = None,
                         category: Optional[str] = None, active: Optional[bool] = None,
                         description: Optional[str] = None):
        gid = str(interaction.guild.id)
        fields = {"name": name, "stock": stock, "weight": weight, "category": category,
                  "active": active, "description": description}
        prize = await self.draws.edit_prize(gid, prize_id, fields)
        if prize is None:
            await interaction.response.send_message(embed=create_error_embed(
                "Not found", "No prize with id `{}`.".format(prize_id)), ephemeral=True)
            return
        await self.referral.audit(gid, str(interaction.user.id), "prize_edited",
                                  {"prize_id": prize_id, "fields": {k: v for k, v in fields.items() if v is not None}})
        self.schedule_panel_refresh(interaction.guild)
        await interaction.response.send_message(embed=create_success_embed(
            "Prize updated", "`{}` **{}** - stock {}, weight {}, {}".format(
                prize["prize_id"], prize["name"], "unlimited" if prize.get("unlimited") else prize["stock"],
                prize["weight"], "active" if prize["active"] else "disabled")), ephemeral=True)

    @prize_group.command(name="remove", description="Remove a prize from the pool")
    @app_commands.describe(prize_id="Prize to remove")
    @staff_only()
    async def prize_remove(self, interaction: discord.Interaction, prize_id: str):
        gid = str(interaction.guild.id)
        ok = await self.draws.remove_prize(gid, prize_id)
        if ok:
            await self.referral.audit(gid, str(interaction.user.id), "prize_removed", {"prize_id": prize_id})
            self.schedule_panel_refresh(interaction.guild)
        await interaction.response.send_message(
            embed=create_success_embed("Prize removed", "`{}` was removed. Past winners are kept.".format(prize_id))
            if ok else create_error_embed("Not found", "No prize with id `{}`.".format(prize_id)), ephemeral=True)

    @prize_group.command(name="inventory", description="Show prizes, stock and winning chances")
    @app_commands.describe(campaign="Campaign id (blank = main lucky draw)")
    @staff_only()
    async def prize_inventory(self, interaction: discord.Interaction, campaign: Optional[str] = None):
        gid = str(interaction.guild.id)
        campaign_id = campaign or DEFAULT_CAMPAIGN
        rows = await self.draws.list_prizes(gid, campaign_id)
        pool = await self.draws.get_pool(gid, campaign_id)
        total = sum(int(p["weight"]) for p in pool) or 1
        if not rows:
            await interaction.response.send_message(embed=_embed(
                "\U0001f4e6 Prize Inventory", "No prizes in this campaign yet. Use `/prize add`."), ephemeral=True)
            return
        pool_ids = {p["prize_id"] for p in pool}
        lines = []
        for p in rows:
            chance = "{:.1f}%".format(100 * int(p["weight"]) / total) if p["prize_id"] in pool_ids else "out"
            lines.append("`{}` **{}**{} - stock {} \u2022 weight {} \u2022 chance {}{}".format(
                p["prize_id"], p["name"], " *(no prize)*" if p.get("is_losing") else "",
                "\u221e" if p.get("unlimited") else p["stock"], p["weight"], chance,
                "" if p.get("active") else " \u2022 disabled"))
        await interaction.response.send_message(
            embed=_embed("\U0001f4e6 Prize Inventory", "\n".join(lines)[:3900]), ephemeral=True)

    @prize_group.command(name="winners", description="List prize winners and delivery status")
    @app_commands.describe(status="Filter by status")
    @app_commands.choices(status=[app_commands.Choice(name=n.title(), value=n)
                                  for n in ("pending", "claimed", "delivered")])
    @staff_only()
    async def prize_winners(self, interaction: discord.Interaction,
                            status: Optional[app_commands.Choice[str]] = None):
        rows = await self.draws.list_winners(str(interaction.guild.id), status.value if status else None)
        if not rows:
            await interaction.response.send_message(embed=_embed("\U0001f3c6 Winners", "No winners found."),
                                                    ephemeral=True)
            return
        lines = ["`{}` <@{}> - **{}** - {} ({})".format(w["winner_id"], w["user_id"], w["prize_name"],
                                                       w["status"], _when(w["created_at"])) for w in rows]
        await interaction.response.send_message(
            embed=_embed("\U0001f3c6 Winners", "\n".join(lines)[:3900]), ephemeral=True)

    @prize_group.command(name="deliver", description="Update a winner's delivery status")
    @app_commands.describe(winner_id="Winner id (from /prize winners)", status="New status", note="Optional note")
    @app_commands.choices(status=[app_commands.Choice(name=n.title(), value=n)
                                  for n in ("pending", "claimed", "delivered")])
    @staff_only()
    async def prize_deliver(self, interaction: discord.Interaction, winner_id: str,
                            status: app_commands.Choice[str], note: Optional[str] = ""):
        gid = str(interaction.guild.id)
        row = await self.draws.set_delivery(gid, winner_id, status.value, str(interaction.user.id), note or "")
        if row is None:
            await interaction.response.send_message(embed=create_error_embed(
                "Not found", "No winner with id `{}`.".format(winner_id)), ephemeral=True)
            return
        await self.referral.audit(gid, str(interaction.user.id), "prize_delivery",
                                  {"winner_id": winner_id, "status": status.value, "note": note or ""})
        await interaction.response.send_message(embed=create_success_embed(
            "Updated", "`{}` ({}) is now **{}**.".format(winner_id, row["prize_name"], status.value)),
            ephemeral=True)

    prize_edit.autocomplete("prize_id")(_prize_autocomplete)
    prize_remove.autocomplete("prize_id")(_prize_autocomplete)

    # ─────────────────────────────────────────────────────────────────────
    # /points  (admin)
    # ─────────────────────────────────────────────────────────────────────

    points_group = app_commands.Group(name="points", description="Adjust reward points (staff)", guild_only=True)

    @points_group.command(name="add", description="Give points to a member")
    @app_commands.describe(member="Who receives the points", amount="How many points", reason="Why (logged)")
    @staff_only()
    async def points_add(self, interaction: discord.Interaction, member: discord.Member,
                         amount: app_commands.Range[int, 1, 100000], reason: str):
        gid = str(interaction.guild.id)
        res = await self.points.add_points(
            gid, str(member.id), amount, txn_type="admin_add",
            description="Staff adjustment by {}: {}".format(interaction.user.id, reason[:150]),
            idempotency_key="admin:{}".format(interaction.id),
            metadata={"actor_id": str(interaction.user.id)})
        await self.referral.audit(gid, str(interaction.user.id), "points_added",
                                  {"user_id": str(member.id), "amount": amount, "reason": reason,
                                   "transaction_id": res.transaction_id})
        await interaction.response.send_message(embed=create_success_embed(
            "Points added", "{} received **{}**. New balance: **{}**.".format(
                member.mention, _pts(amount), res.balance_after)), ephemeral=True)

    @points_group.command(name="remove", description="Take points from a member")
    @app_commands.describe(member="Who loses the points", amount="How many points", reason="Why (logged)")
    @staff_only()
    async def points_remove(self, interaction: discord.Interaction, member: discord.Member,
                            amount: app_commands.Range[int, 1, 100000], reason: str):
        gid = str(interaction.guild.id)
        res = await self.points.deduct_points(
            gid, str(member.id), amount, txn_type="admin_remove",
            description="Staff adjustment by {}: {}".format(interaction.user.id, reason[:150]),
            idempotency_key="admin:{}".format(interaction.id),
            metadata={"actor_id": str(interaction.user.id)})
        await self.referral.audit(gid, str(interaction.user.id), "points_removed",
                                  {"user_id": str(member.id), "amount": amount, "reason": reason,
                                   "ok": res.ok, "transaction_id": res.transaction_id})
        if not res.ok:
            await interaction.response.send_message(embed=create_error_embed(
                "Not enough points", "{} only has **{}**.".format(member.mention, _pts(res.balance_after or 0))),
                ephemeral=True)
            return
        await interaction.response.send_message(embed=create_success_embed(
            "Points removed", "Removed **{}** from {}. New balance: **{}**.".format(
                _pts(amount), member.mention, res.balance_after)), ephemeral=True)

    @points_group.command(name="check", description="Look at a member's points and recent activity")
    @app_commands.describe(member="Member to check")
    @staff_only()
    async def points_check(self, interaction: discord.Interaction, member: discord.Member):
        gid = str(interaction.guild.id)
        s = await self.points.get_user_stats(gid, str(member.id))
        history = await self.points.get_transaction_history(gid, str(member.id), 8)
        embed = _embed("\U0001f50d Points: {}".format(member.display_name),
                       "Balance **{}** \u2022 earned {} \u2022 spent {}".format(
                           s["points_balance"], s["total_points_earned"], s["total_points_spent"]))
        embed.add_field(name="Referral / chat", value="{} / {}".format(s["referral_points"], s["chat_points"]))
        embed.add_field(name="Valid referrals", value=str(s["total_valid_referrals"]))
        embed.add_field(name="Draw entries used", value=str(s["total_draw_entries"]))
        if history:
            embed.add_field(name="Recent", inline=False, value="\n".join(
                "`{:+d}` {} \u2022 {}".format(h["amount"], h["description"][:60], _when(h["created_at"]))
                for h in history)[:1000])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────
    # /draw  (admin)
    # ─────────────────────────────────────────────────────────────────────

    draw_group = app_commands.Group(name="draw", description="Lucky draw campaigns (staff)", guild_only=True)

    @draw_group.command(name="create", description="Create a lucky draw campaign (starts as a draft)")
    @app_commands.describe(name="Campaign name", draw_type="Instant draw or raffle",
                           max_entries_per_user="Raffle only: tickets one member may buy (0 = unlimited)")
    @app_commands.choices(draw_type=[app_commands.Choice(name="Instant draw", value="instant"),
                                     app_commands.Choice(name="Campaign raffle", value="raffle")])
    @staff_only()
    async def draw_create(self, interaction: discord.Interaction, name: str,
                          draw_type: app_commands.Choice[str],
                          max_entries_per_user: app_commands.Range[int, 0, 100000] = 0):
        gid = str(interaction.guild.id)
        draw = await self.draws.create_draw(gid, name, draw_type.value,
                                            max_entries_per_user=max_entries_per_user,
                                            actor_id=str(interaction.user.id))
        await self.referral.audit(gid, str(interaction.user.id), "draw_created",
                                  {"draw_id": draw["draw_id"], "name": name, "type": draw_type.value})
        await interaction.response.send_message(embed=create_success_embed(
            "Campaign created", "`{}` **{}** ({}).\nAdd prizes with `/prize add campaign:{}` then "
                                "`/draw start`.".format(draw["draw_id"], draw["name"], draw_type.name,
                                                        draw["draw_id"])), ephemeral=True)

    @draw_group.command(name="start", description="Open a campaign for entries")
    @app_commands.describe(draw_id="Campaign to open")
    @staff_only()
    async def draw_start(self, interaction: discord.Interaction, draw_id: str):
        gid = str(interaction.guild.id)
        draw = await self.draws.get_draw(gid, draw_id)
        if draw is None or not await self.draws.start_draw(gid, draw_id):
            await interaction.response.send_message(embed=create_error_embed(
                "Cannot start", "Only draft campaigns can be started."), ephemeral=True)
            return
        await self.referral.audit(gid, str(interaction.user.id), "draw_started", {"draw_id": draw_id})
        await interaction.response.send_message(embed=create_success_embed(
            "Campaign open", "**{}** is open. Members join with `/luckydraw campaign:{}`.".format(
                draw["name"], draw["name"])), ephemeral=True)

    @draw_group.command(name="close", description="Close a campaign (raffle: draws the winners now)")
    @app_commands.describe(draw_id="Campaign to close")
    @staff_only()
    async def draw_close(self, interaction: discord.Interaction, draw_id: str):
        gid = str(interaction.guild.id)
        await interaction.response.defer(ephemeral=True)
        draw = await self.draws.get_draw(gid, draw_id)
        if draw is None:
            await interaction.followup.send(embed=create_error_embed("Not found", "Unknown campaign."), ephemeral=True)
            return
        if draw["type"] == "raffle":
            winners = await self.draws.close_raffle(gid, draw_id)
            if winners is None:
                await interaction.followup.send(embed=create_error_embed(
                    "Cannot close", "Only open raffles can be closed."), ephemeral=True)
                return
            await self.referral.audit(gid, str(interaction.user.id), "raffle_closed",
                                      {"draw_id": draw_id, "winners": len(winners)})
            lines = ["<@{}> - **{}**".format(w["user_id"], w["prize_name"]) for w in winners] or ["No winners (no entries or no prizes)."]
            await interaction.followup.send(embed=_embed("\U0001f3c6 Raffle closed: {}".format(draw["name"]),
                                                         "\n".join(lines)[:3900]), ephemeral=True)
            return
        res = await self.db_close_instant(gid, draw_id)
        await self.referral.audit(gid, str(interaction.user.id), "draw_closed", {"draw_id": draw_id})
        await interaction.followup.send(embed=create_success_embed(
            "Campaign closed" if res else "Cannot close", "The instant draw is closed." if res
            else "Only open campaigns can be closed."), ephemeral=True)

    async def db_close_instant(self, guild_id: str, draw_id: str) -> bool:
        from utils.reward_common import utcnow
        res = await self.bot.db.points_draws.update_one(
            {"guild_id": guild_id, "draw_id": draw_id, "type": "instant", "status": "open"},
            {"$set": {"status": "closed", "closed_at": utcnow()}})
        return res.modified_count == 1

    @draw_group.command(name="results", description="Show a campaign's results (or list campaigns)")
    @app_commands.describe(draw_id="Campaign (blank = list recent campaigns)")
    @staff_only()
    async def draw_results(self, interaction: discord.Interaction, draw_id: Optional[str] = None):
        gid = str(interaction.guild.id)
        if not draw_id:
            rows = await self.draws.list_draws(gid)
            text = "\n".join("`{}` **{}** - {} / {}".format(d["draw_id"], d["name"], d["type"], d["status"])
                             for d in rows) or "No campaigns yet. Create one with `/draw create`."
            await interaction.response.send_message(embed=_embed("\U0001f3b2 Campaigns", text[:3900]), ephemeral=True)
            return
        data = await self.draws.draw_results(gid, draw_id)
        if not data:
            await interaction.response.send_message(embed=create_error_embed("Not found", "Unknown campaign."),
                                                    ephemeral=True)
            return
        d = data["draw"]
        embed = _embed("\U0001f3b2 {}".format(d["name"]),
                       "Type **{}** \u2022 status **{}**\nParticipants **{}** \u2022 entries **{}**".format(
                           d["type"], d["status"], data["participants"], data["entries"]))
        if data["winners"]:
            embed.add_field(name="Winners", inline=False, value="\n".join(
                "`{}` <@{}> - **{}** ({})".format(w["winner_id"], w["user_id"], w["prize_name"], w["status"])
                for w in data["winners"])[:1000])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @draw_group.command(name="cancel", description="Cancel a campaign (raffle entries are refunded)")
    @app_commands.describe(draw_id="Campaign to cancel")
    @staff_only()
    async def draw_cancel(self, interaction: discord.Interaction, draw_id: str):
        gid = str(interaction.guild.id)
        await interaction.response.defer(ephemeral=True)
        refunded = await self.draws.cancel_draw(gid, draw_id)
        if refunded is None:
            await interaction.followup.send(embed=create_error_embed(
                "Cannot cancel", "Only draft or open campaigns can be cancelled."), ephemeral=True)
            return
        await self.referral.audit(gid, str(interaction.user.id), "draw_cancelled",
                                  {"draw_id": draw_id, "refunded_points": refunded})
        await interaction.followup.send(embed=create_success_embed(
            "Campaign cancelled", "Refunded **{}** to participants.".format(_pts(refunded))), ephemeral=True)

    draw_start.autocomplete("draw_id")(_draw_autocomplete)
    draw_close.autocomplete("draw_id")(_draw_autocomplete)
    draw_results.autocomplete("draw_id")(_draw_autocomplete)
    draw_cancel.autocomplete("draw_id")(_draw_autocomplete)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Referral(bot))
