import asyncio
import glob
import inspect
import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import bootstrap  # noqa: F401
import discord
from discord import app_commands
from fake_mongo import FakeDatabase
from fakes import FakeGuild, FakeMember, make_interaction
from utils.reward_migrations import run_pending_migrations

import cogs.referral as R

ROOT = bootstrap.ROOT
G = "1"


def _own_top_level():
    out = {}
    for name in dir(R.Referral):
        obj = getattr(R.Referral, name)
        if isinstance(obj, (app_commands.Command, app_commands.Group)):
            out[obj.name] = obj
    return out


class StructureTests(unittest.TestCase):
    def test_no_name_collision_with_existing_commands(self):
        existing = set()
        for path in glob.glob(os.path.join(ROOT, "cogs", "*.py")):
            if path.endswith("referral.py"):
                continue
            src = open(path, encoding="utf-8").read()
            existing |= set(re.findall(r'app_commands\.command\(\s*name="([^"]+)"', src))
            existing |= set(re.findall(r'Group\(\s*name="([^"]+)"', src))
        mine = set(_own_top_level())
        self.assertGreater(len(existing), 20)                         # sanity: really scanned the project
        self.assertEqual(mine & existing, set(), "duplicate command names")
        self.assertIn("leaderboard", existing)                        # the reason /pointsboard exists
        self.assertNotIn("leaderboard", mine)
        self.assertLessEqual(len(existing | mine), 100)               # Discord's global command cap

    def test_expected_commands_exist(self):
        top = _own_top_level()
        for name in ("myinvite", "mypoints", "luckydraw", "myrewards", "pointsboard",
                     "referral", "prize", "points", "draw"):
            self.assertIn(name, top)
        subs = {g: {c.name for c in top[g].commands} for g in ("referral", "prize", "points", "draw")}
        self.assertTrue({"setup", "enable", "disable", "stats", "review", "config"} <= subs["referral"])
        self.assertTrue({"add", "edit", "remove", "inventory"} <= subs["prize"])
        self.assertEqual(subs["points"], {"add", "remove", "check"})
        self.assertEqual(subs["draw"], {"create", "start", "close", "results", "cancel"})

    def test_discord_limits_respected(self):
        pattern = re.compile(r"^[-_\w]{1,32}$")
        cmds = []
        for obj in _own_top_level().values():
            if isinstance(obj, app_commands.Group):
                self.assertLessEqual(len(obj.commands), 25)
                self.assertLessEqual(len(obj.description), 100)
                cmds.extend(obj.commands)
            else:
                cmds.append(obj)
        for c in cmds:
            self.assertRegex(c.name, pattern)
            self.assertEqual(c.name, c.name.lower())
            self.assertTrue(1 <= len(c.description) <= 100, c.name)
            params = set(inspect.signature(c.callback).parameters)
            described = getattr(c.callback, "__describe__", None) or \
                getattr(c.callback, "__discord_app_commands_param_description__", {})
            for key, text in described.items():
                self.assertIn(key, params, "{}: describe() for unknown parameter {}".format(c.name, key))
                self.assertLessEqual(len(text), 100, "{}.{}".format(c.name, key))
            chosen = getattr(c.callback, "__choices__", None) or {}
            for key in chosen:
                self.assertIn(key, params)

    def test_panel_buttons_are_persistent_with_unique_ids(self):
        metas = [getattr(f, "__button_meta__", None) for f in vars(R.ReferralPanelView).values()]
        metas = [m for m in metas if m]
        if not metas:
            self.skipTest("real discord.py wraps buttons differently; custom ids checked in staging")
        ids = [m["custom_id"] for m in metas]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)
        self.assertTrue(all(i.startswith("wwmref:") for i in ids))
        self.assertEqual({m["label"] for m in metas},
                         {"My Points", "Lucky Draw Now", "My Invite Link", "My Rewards", "Leaderboard"})
        self.assertTrue(all(len(i) <= 100 for i in ids))
        view = R.ReferralPanelView(MagicMock())
        self.assertIsNone(view.timeout)                               # persistent


class CogCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = FakeDatabase()
        await run_pending_migrations(self.db)
        self.db.rewards_ready = True
        self.guild = FakeGuild(1)
        self.admin = self.guild.add_member(user_id=1, age_days=900)
        self.admin.guild_permissions.administrator = True
        self.user = self.guild.add_member(user_id=2, age_days=100)
        self.bot = SimpleNamespace(db=self.db, add_view=MagicMock(), guilds=[self.guild],
                                   wait_until_ready=AsyncMock())
        self.cog = R.Referral(self.bot)
        self.bot.get_cog = lambda name: self.cog if name == "Referral" else None
        await self.cog.cog_load()
        for t in self.cog._tasks:
            t.cancel()
        self.cog._tasks.clear()
        await self.cog.referral.update_config(G, {"channel_ids": [self.guild.channel.id]})

    async def asyncTearDown(self):
        self.cog.cog_unload()

    def it(self, member=None, iid=None):
        i = make_interaction(self.guild, member or self.user, iid)
        i.client.get_cog = self.bot.get_cog
        return i

    async def fund(self, user, n, key=None):
        await self.cog.points.add_points(G, str(user.id), n, txn_type="admin_add", description="seed",
                                         idempotency_key=key or "seed:%s" % user.id)

    async def stock_pool(self, stock=5):
        await self.cog.draws.add_prize(G, "G-Star Ticket", stock=stock, weight=1)
        await self.cog.draws.add_prize(G, "No prize", stock=0, weight=0, is_losing=True)

    @staticmethod
    def call(command, cog, *args, **kw):
        return command.callback(cog, *args, **kw)


class PanelAndMemberFlows(CogCase):
    async def test_cog_refuses_to_load_when_migrations_failed(self):
        self.db.rewards_ready = False
        with self.assertRaises(RuntimeError):
            await R.Referral(self.bot).cog_load()

    async def test_persistent_view_registered_on_startup(self):
        self.bot.add_view.assert_called_once()

    async def test_panel_text_and_dynamic_prize_pool(self):
        await self.stock_pool(5)
        cfg = await self.cog.referral.get_config(G)
        e = await self.cog.build_panel_embed(self.guild, cfg)
        self.assertEqual(e.title, "\U0001f48e Invite Friends & Earn Rewards")
        self.assertIn("Got friends who would love our community? Invite them and let's grow together!", e.description)
        self.assertIn("The more friends you bring, the more chances you earn!", e.description)
        text = "\n".join(f["value"] for f in e.fields)
        for line in ("3 points per valid referral.", "1 point for your first eligible chat message",
                     "1 point = 1 lucky draw entry.", "Minimum account age: 7 days."):
            self.assertIn(line, text)
        self.assertIn("**5x** G-Star Ticket", text)
        self.assertNotIn("No prize", text)                           # losing outcomes are never advertised
        await self.cog.draws.edit_prize(G, (await self.cog.draws.list_prizes(G))[0]["prize_id"], {"stock": 2})
        e2 = await self.cog.build_panel_embed(self.guild, cfg)
        self.assertIn("**2x** G-Star Ticket", "\n".join(f["value"] for f in e2.fields))

    async def test_my_points_shows_correct_values(self):
        await self.cog.points.add_points(G, "2", 3, txn_type="referral", description="r", idempotency_key="r",
                                         bucket="referral_points", extra_inc={"total_valid_referrals": 1})
        await self.cog.points.add_points(G, "2", 1, txn_type="chat", description="c", idempotency_key="c",
                                         bucket="chat_points")
        await self.cog.points.deduct_points(G, "2", 1, txn_type="draw_spend", description="d",
                                            idempotency_key="d", extra_inc={"total_draw_entries": 1})
        it = self.it()
        await self.cog.guarded(it, self.cog.show_points)
        embed = it.sent[-1]["embed"]
        self.assertTrue(it.sent[-1]["ephemeral"])
        self.assertIn("3 points", embed.description)
        values = {f["name"]: f["value"] for f in embed.fields}
        self.assertEqual((values["Referral points"], values["Chat points"], values["Total earned"],
                          values["Total spent"], values["Valid referrals"], values["Draw entries used"]),
                         ("3", "1", "4", "1", "1", "1"))

    async def test_lucky_draw_win_loss_and_insufficient(self):
        await self.stock_pool(2)
        it = self.it()
        await self.cog.guarded(it, self.cog.do_lucky_draw, 1, None)
        self.assertIn("do not have enough points", it.sent[-1]["embed"].description)
        await self.fund(self.user, 3)
        it = self.it()
        await self.cog.guarded(it, self.cog.do_lucky_draw, 3, None)
        embed = it.sent[-1]["embed"]
        self.assertEqual(embed.title, "\U0001f389 Congratulations!")           # both units of stock won
        self.assertIn("Refund", {f["name"] for f in embed.fields})            # pool ran dry on entry 3
        self.assertEqual(await self.cog.points.get_balance(G, "2"), 1)        # unplayed entry refunded

    async def test_loss_message_matches_reference_wording(self):
        await self.cog.draws.add_prize(G, "Better luck", stock=0, weight=1, is_losing=True)
        await self.fund(self.user, 1)
        it = self.it()
        await self.cog.guarded(it, self.cog.do_lucky_draw, 1, None)
        self.assertIn("Better luck next time!", it.sent[-1]["embed"].description)

    async def test_win_message_and_delivery_flow(self):
        await self.cog.draws.add_prize(G, "G-Star Ticket", stock=1, weight=1)
        await self.fund(self.user, 1)
        it = self.it()
        await self.cog.guarded(it, self.cog.do_lucky_draw, 1, None)
        self.assertIn("G-Star Ticket", it.sent[-1]["embed"].description)
        it = self.it()
        await self.cog.guarded(it, self.cog.show_rewards)
        text = "\n".join(f["value"] for f in it.sent[-1]["embed"].fields)
        self.assertIn("G-Star Ticket", text)
        self.assertIn("Pending", text)
        self.assertIn("view", it.sent[-1])                           # claim button offered

    async def test_failed_reply_never_causes_a_second_draw(self):
        await self.stock_pool(5)
        await self.fund(self.user, 2)
        it = self.it(iid=777)
        it.followup.send.side_effect = Exception("Discord is down")
        await self.cog.guarded(it, self.cog.do_lucky_draw, 1, None)          # reply fails, result saved
        self.assertEqual(await self.cog.points.get_balance(G, "2"), 1)
        again = self.it(iid=777)
        await self.cog.guarded(again, self.cog.do_lucky_draw, 1, None)       # same interaction repeated
        self.assertEqual(await self.cog.points.get_balance(G, "2"), 1)
        self.assertIn("original result", "".join(f["value"] for f in again.sent[-1]["embed"].fields))

    async def test_leaderboard_and_empty_leaderboard(self):
        it = self.it()
        await self.cog.guarded(it, self.cog.show_board)
        self.assertIn("Nobody has earned points yet", it.sent[-1]["embed"].description)
        await self.fund(self.user, 5)
        it = self.it()
        await self.cog.guarded(it, self.cog.show_board)
        self.assertIn("<@2>", it.sent[-1]["embed"].description)

    async def test_myinvite_creates_reuses_and_reports_missing_permission(self):
        it = self.it()
        await self.cog.guarded(it, self.cog.show_invite)
        url = it.sent[-1]["embed"].description
        self.assertTrue(url.startswith("https://discord.gg/"))
        it2 = self.it()
        await self.cog.guarded(it2, self.cog.show_invite)
        self.assertEqual(it2.sent[-1]["embed"].description, url)
        self.assertEqual(len(self.guild.live), 1)
        labels = {f["name"] for f in it.sent[-1]["embed"].fields}
        self.assertTrue({"Valid referrals", "Pending review", "Referral points earned"} <= labels)

        broken_guild = FakeGuild(5)
        broken_guild.channel.create_invite = AsyncMock(side_effect=discord.Forbidden())
        member = broken_guild.add_member(user_id=44)
        it3 = make_interaction(broken_guild, member)
        await self.cog.guarded(it3, self.cog.show_invite)
        self.assertIn("Create Invite", it3.sent[-1]["embed"].description)

    async def test_disabled_system_pauses_member_features(self):
        await self.cog.referral.update_config(G, {"enabled": False})
        it = self.it()
        await self.cog.guarded(it, self.cog.show_points)
        self.assertEqual(it.sent[-1]["embed"].title, "Rewards are paused")

    async def test_unexpected_error_is_reported_politely(self):
        async def boom(*a):
            raise RuntimeError("db down")
        it = self.it()
        await self.cog.guarded(it, boom)
        self.assertIn("Your points are safe", it.sent[-1]["embed"].description)


class ListenerTests(CogCase):
    def message(self, author, channel=None, **kw):
        base = dict(author=author, webhook_id=None, guild=self.guild, channel=channel or self.guild.channel,
                    type=discord.MessageType.default)
        base.update(kw)
        return SimpleNamespace(**base)

    async def test_first_eligible_message_pays_once(self):
        await self.cog.on_message(self.message(self.user))
        await self.cog.on_message(self.message(self.user))
        await self.cog.on_message(self.message(self.user))
        self.assertEqual(await self.cog.points.get_balance(G, "2"), 1)

    async def test_ignored_messages(self):
        bot = self.guild.add_member(user_id=50, bot=True)
        for msg in (self.message(bot),
                    self.message(self.user, webhook_id=123),
                    self.message(self.user, guild=None),
                    self.message(self.user, type=discord.MessageType.join),
                    self.message(self.user, channel=SimpleNamespace(id=424242, parent_id=None))):
            await self.cog.on_message(msg)
        self.assertEqual(await self.cog.points.get_balance(G, "2"), 0)

    async def test_member_join_awards_inviter_and_announces(self):
        ann = self.guild.channel
        await self.cog.referral.update_config(G, {"announcement_channel_id": ann.id})
        inviter = self.guild.add_member(user_id=60, age_days=500)
        inv = self.guild.add_invite("XYZ", inviter=inviter)
        await self.cog.tracker.sync_guild(self.guild)
        newcomer = self.guild.add_member(user_id=61, age_days=40)
        inv.uses += 1
        await self.cog.on_member_join(newcomer)
        self.assertEqual(await self.cog.points.get_balance(G, "60"), 3)
        self.assertEqual(len(ann.sent), 1)
        await self.cog.on_member_join(newcomer)                      # duplicate event
        self.assertEqual(await self.cog.points.get_balance(G, "60"), 3)

    async def test_bot_join_ignored(self):
        await self.cog.tracker.sync_guild(self.guild)
        await self.cog.on_member_join(self.guild.add_member(user_id=70, bot=True))
        self.assertEqual(await self.db.referrals.count_documents({}), 0)

    async def test_listener_errors_do_not_propagate(self):
        self.cog.referral.claim_chat_reward = AsyncMock(side_effect=RuntimeError("x"))
        await self.cog.on_message(self.message(self.user))           # must not raise


class AdminTests(CogCase):
    async def test_staff_check(self):
        def predicate():
            fn = R.staff_only()(lambda: None)
            return (getattr(fn, "__checks__", None) or fn.__discord_app_commands_checks__)[0]
        pred = predicate()
        self.assertTrue(await pred(self.it(self.admin)))
        it = self.it(self.user)
        self.assertFalse(await pred(it))
        self.assertIn("not allowed", it.sent[-1]["embed"].description)
        role = SimpleNamespace(id=900)
        helper = self.guild.add_member(user_id=3)
        helper.roles = [role]
        self.assertFalse(await pred(self.it(helper)))
        await self.cog.referral.update_config(G, {}, add_to={"admin_role_ids": [900]})
        self.assertTrue(await pred(self.it(helper)))

    async def test_every_admin_command_is_protected(self):
        for group in ("referral", "prize", "points", "draw"):
            for cmd in _own_top_level()[group].commands:
                checks = getattr(cmd.callback, "__checks__", None) or \
                    getattr(cmd.callback, "__discord_app_commands_checks__", None)
                self.assertTrue(checks, "{}/{} has no permission check".format(group, cmd.name))

    async def test_prize_lifecycle_and_inventory(self):
        top = _own_top_level()
        prize = top["prize"]
        cmd = {c.name: c for c in prize.commands}
        it = self.it(self.admin)
        await self.call(cmd["add"], self.cog, it, "G-Star Ticket", 20, 5, "ticket", False, None, "")
        await self.call(cmd["add"], self.cog, it, "Thanks!", 0, 95, "none", True, None, "")
        rows = await self.cog.draws.list_prizes(G)
        self.assertEqual(len(rows), 2)
        ticket = next(r for r in rows if r["name"] == "G-Star Ticket")
        await self.call(cmd["edit"], self.cog, it, ticket["prize_id"], None, 15, None, None, None, None)
        await self.call(cmd["inventory"], self.cog, it, None)
        text = it.sent[-1]["embed"].description
        self.assertIn("stock 15", text)
        self.assertIn("chance 5.0%", text)
        self.assertIn("chance 95.0%", text)
        await self.call(cmd["remove"], self.cog, it, ticket["prize_id"])
        await self.call(cmd["remove"], self.cog, it, ticket["prize_id"])
        self.assertIn("Not found", it.sent[-1]["embed"].title)
        audits = [a["action"] for a in await self.db.referral_audit_logs.find({}).to_list(100)]
        self.assertEqual(audits.count("prize_added"), 2)
        self.assertIn("prize_removed", audits)

    async def test_manual_point_adjustments_are_logged_and_safe(self):
        cmd = {c.name: c for c in _own_top_level()["points"].commands}
        it = self.it(self.admin, iid=1)
        await self.call(cmd["add"], self.cog, it, self.user, 10, "event reward")
        self.assertEqual(await self.cog.points.get_balance(G, "2"), 10)
        await self.call(cmd["remove"], self.cog, self.it(self.admin, iid=2), self.user, 4, "correction")
        self.assertEqual(await self.cog.points.get_balance(G, "2"), 6)
        over = self.it(self.admin, iid=3)
        await self.call(cmd["remove"], self.cog, over, self.user, 99, "too much")
        self.assertIn("Not enough points", over.sent[-1]["embed"].title)
        self.assertEqual(await self.cog.points.get_balance(G, "2"), 6)
        await self.call(cmd["add"], self.cog, self.it(self.admin, iid=1), self.user, 10, "event reward")  # replay
        self.assertEqual(await self.cog.points.get_balance(G, "2"), 6)
        chk = self.it(self.admin)
        await self.call(cmd["check"], self.cog, chk, self.user)
        self.assertIn("Balance **6**", chk.sent[-1]["embed"].description)
        logs = await self.db.referral_audit_logs.find({}).to_list(100)
        self.assertEqual(sorted(a["action"] for a in logs if a["action"].startswith("points_")),
                         ["points_added", "points_added", "points_removed", "points_removed"])
        reasons = [a["details"]["reason"] for a in logs if "reason" in a["details"]]
        self.assertIn("event reward", reasons)

    async def test_draw_campaign_commands_end_to_end(self):
        cmd = {c.name: c for c in _own_top_level()["draw"].commands}
        pcmd = {c.name: c for c in _own_top_level()["prize"].commands}
        it = self.it(self.admin)
        raffle = app_commands.Choice(name="Campaign raffle", value="raffle")
        await self.call(cmd["create"], self.cog, it, "Weekend raffle", raffle, 3)
        draw = (await self.cog.draws.list_draws(G))[0]
        await self.call(pcmd["add"], self.cog, it, "Skin", 1, 1, "item", False, draw["draw_id"], "")
        await self.call(cmd["start"], self.cog, it, draw["draw_id"])
        await self.fund(self.user, 5)
        member_it = self.it(self.user)
        await self.cog.guarded(member_it, self.cog.do_lucky_draw, 2, draw["draw_id"])
        self.assertIn("ticket", member_it.sent[-1]["embed"].description)
        await self.call(cmd["results"], self.cog, it, draw["draw_id"])
        self.assertIn("entries **2**", it.sent[-1]["embed"].description)
        await self.call(cmd["close"], self.cog, it, draw["draw_id"])
        self.assertIn("<@2>", it.sent[-1]["embed"].description)
        await self.call(cmd["cancel"], self.cog, it, draw["draw_id"])
        self.assertIn("Cannot cancel", it.sent[-1]["embed"].title)
        winners = {c.name: c for c in _own_top_level()["prize"].commands}
        wid = (await self.cog.draws.list_winners(G))[0]["winner_id"]
        await self.call(winners["deliver"], self.cog, it, wid, app_commands.Choice(name="Delivered", value="delivered"), "sent")
        self.assertEqual((await self.cog.draws.list_winners(G))[0]["status"], "delivered")

    async def test_cancel_open_raffle_refunds(self):
        cmd = {c.name: c for c in _own_top_level()["draw"].commands}
        draw = await self.cog.draws.create_draw(G, "Temp", "raffle")
        await self.cog.draws.start_draw(G, draw["draw_id"])
        await self.fund(self.user, 4)
        await self.cog.guarded(self.it(self.user), self.cog.do_lucky_draw, 4, draw["draw_id"])
        self.assertEqual(await self.cog.points.get_balance(G, "2"), 0)
        await self.call(cmd["cancel"], self.cog, self.it(self.admin), draw["draw_id"])
        self.assertEqual(await self.cog.points.get_balance(G, "2"), 4)

    async def test_referral_config_enable_disable_stats_review(self):
        cmd = {c.name: c for c in _own_top_level()["referral"].commands}
        it = self.it(self.admin)
        extra = self.guild.channel
        await self.call(cmd["config"], self.cog, it, 5, 2, 10, extra, None, None, None, "", 20, None, None)
        cfg = await self.cog.referral.get_config(G)
        self.assertEqual((cfg.referral_points, cfg.chat_points, cfg.min_account_age_days, cfg.max_draw_entries),
                         (5, 2, 10, 20))
        self.assertIn(extra.id, cfg.channel_ids)
        await self.call(cmd["disable"], self.cog, it)
        self.assertFalse((await self.cog.referral.get_config(G)).enabled)
        await self.call(cmd["enable"], self.cog, it)
        self.assertTrue((await self.cog.referral.get_config(G)).enabled)
        await self.call(cmd["stats"], self.cog, it)
        self.assertEqual(it.sent[-1]["embed"].title, "\U0001f4c8 Referral Stats")
        review = app_commands.Choice(name="List pending", value="list")
        await self.call(cmd["review"], self.cog, it, review, None, None)
        self.assertIn("Nothing is waiting", it.sent[-1]["embed"].description)

    async def test_setup_posts_panel_and_reuses_message(self):
        cmd = {c.name: c for c in _own_top_level()["referral"].commands}
        ch = self.guild.channel
        message = MagicMock()
        message.id = 9001
        message.edit = AsyncMock()
        ch.send = AsyncMock(return_value=message)
        ch.fetch_message = AsyncMock(return_value=message)
        it = self.it(self.admin)
        await self.call(cmd["setup"], self.cog, it, ch)
        ch.send.assert_awaited_once()
        cfg = await self.cog.referral.get_config(G)
        self.assertEqual((cfg.panel_channel_id, cfg.panel_message_id), (ch.id, 9001))
        await self.call(cmd["setup"], self.cog, it, ch)               # second run edits instead of re-posting
        ch.send.assert_awaited_once()
        message.edit.assert_awaited()


if __name__ == "__main__":
    unittest.main()
