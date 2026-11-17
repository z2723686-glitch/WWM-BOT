import asyncio
import unittest

import bootstrap  # noqa: F401
import discord
from fake_mongo import FakeDatabase
from fakes import FakeGuild
from utils.invite_tracker import InviteTracker
from utils.points_service import PointsService
from utils.referral_service import ReferralService
from utils.reward_migrations import run_pending_migrations

G = "1"


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = FakeDatabase()
        await run_pending_migrations(self.db)
        self.points = PointsService(self.db)
        self.ref = ReferralService(self.db, self.points)
        self.tracker = InviteTracker(self.db)
        self.guild = FakeGuild(1)
        self.alice = self.guild.add_member(user_id=11, age_days=400)
        self.bob = self.guild.add_member(user_id=12, age_days=400)
        await self.ref.update_config(G, {"channel_ids": [555], "enabled": True})

    async def join(self, via, **kw):
        """Simulate a new member joining through invite `via` (an invite object or None)."""
        member = self.guild.add_member(**kw)
        if via is not None:
            via.uses += 1
        att = await self.tracker.resolve_join(self.guild, member)
        in_guild = bool(att.inviter_id and self.guild.get_member(att.inviter_id))
        doc, awarded = await self.ref.process_join(G, member, att, in_guild)
        return member, att, doc, awarded

    async def balance(self, uid):
        return await self.points.get_balance(G, str(uid))


class ReferralRules(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.inv_a = self.guild.add_invite("AAA", inviter=self.alice)
        self.inv_b = self.guild.add_invite("BBB", inviter=self.bob)
        await self.tracker.sync_guild(self.guild)

    async def test_valid_referral_awards_exactly_three(self):
        m, att, doc, awarded = await self.join(self.inv_a, age_days=30)
        self.assertEqual((att.status, att.inviter_id), ("single", 11))
        self.assertTrue(awarded)
        self.assertEqual(doc["validation_status"], "valid")
        self.assertEqual(doc["reward_status"], "awarded")
        self.assertEqual(await self.balance(11), 3)
        stats = await self.points.get_user_stats(G, "11")
        self.assertEqual((stats["referral_points"], stats["total_valid_referrals"]), (3, 1))

    async def test_new_account_gets_nothing(self):
        m, att, doc, awarded = await self.join(self.inv_a, age_days=3)
        self.assertFalse(awarded)
        self.assertEqual(doc["validation_status"], "invalid_account_age")
        self.assertEqual(await self.balance(11), 0)

    async def test_account_exactly_seven_days_old_counts(self):
        m, att, doc, awarded = await self.join(self.inv_a, age_days=7.01)
        self.assertTrue(awarded)

    async def test_self_referral_rejected(self):
        member = self.guild.add_member(user_id=77, age_days=100)
        self.inv_a.uses += 1
        self.inv_a.inviter = member                       # the joiner owns the invite
        await self.tracker.sync_guild(self.guild)         # (snapshot knows the owner...)
        self.inv_a.uses += 1
        att = await self.tracker.resolve_join(self.guild, member)
        doc, awarded = await self.ref.process_join(G, member, att, True)
        self.assertFalse(awarded)
        self.assertEqual(doc["validation_status"], "invalid_self")
        self.assertEqual(await self.balance(77), 0)

    async def test_bot_accounts_do_not_qualify(self):
        member, att, doc, awarded = await self.join(self.inv_a, bot=True, age_days=500)
        self.assertIsNone(doc)
        self.assertEqual(await self.balance(11), 0)

    async def test_duplicate_referral_pays_once_even_on_rejoin(self):
        m, att, doc, awarded = await self.join(self.inv_a, user_id=300, age_days=30)
        self.assertTrue(awarded)
        # same person leaves and rejoins through another invite
        self.inv_b.uses += 1
        att2 = await self.tracker.resolve_join(self.guild, m)
        doc2, awarded2 = await self.ref.process_join(G, m, att2, True)
        self.assertFalse(awarded2)
        self.assertEqual(await self.balance(11), 3)
        self.assertEqual(await self.balance(12), 0)
        self.assertEqual(await self.db.referrals.count_documents({"invited_user_id": "300"}), 1)

    async def test_same_member_processed_twice_concurrently_pays_once(self):
        member = self.guild.add_member(user_id=301, age_days=30)
        self.inv_a.uses += 1
        att = await self.tracker.resolve_join(self.guild, member)
        out = await asyncio.gather(*[self.ref.process_join(G, member, att, True) for _ in range(5)])
        self.assertEqual(sum(1 for _, awarded in out if awarded), 1)
        self.assertEqual(await self.balance(11), 3)

    async def test_two_simultaneous_joins_same_invite_both_credited_correctly(self):
        m1 = self.guild.add_member(user_id=401, age_days=30)
        m2 = self.guild.add_member(user_id=402, age_days=30)
        self.inv_a.uses += 2                               # both joined before either was processed
        atts = await asyncio.gather(self.tracker.resolve_join(self.guild, m1),
                                    self.tracker.resolve_join(self.guild, m2))
        self.assertEqual([a.status for a in atts], ["single", "single"])
        self.assertEqual({a.inviter_id for a in atts}, {11})
        await asyncio.gather(*[self.ref.process_join(G, m, a, True) for m, a in zip((m1, m2), atts)])
        self.assertEqual(await self.balance(11), 6)

    async def test_two_simultaneous_joins_different_invites_are_not_guessed(self):
        m1 = self.guild.add_member(user_id=403, age_days=30)
        m2 = self.guild.add_member(user_id=404, age_days=30)
        self.inv_a.uses += 1
        self.inv_b.uses += 1
        atts = await asyncio.gather(self.tracker.resolve_join(self.guild, m1),
                                    self.tracker.resolve_join(self.guild, m2))
        self.assertTrue(all(a.status in ("ambiguous", "none") for a in atts))
        for m, a in zip((m1, m2), atts):
            doc, awarded = await self.ref.process_join(G, m, a, True)
            self.assertFalse(awarded)
            self.assertEqual(doc["validation_status"], "pending_review")
        self.assertEqual(await self.balance(11) + await self.balance(12), 0)

    async def test_no_invite_change_goes_to_review_not_guessed(self):
        m, att, doc, awarded = await self.join(None, age_days=30)
        self.assertEqual(att.status, "none")
        self.assertEqual(doc["validation_status"], "pending_review")
        self.assertFalse(awarded)

    async def test_single_use_invite_deleted_after_last_use(self):
        one_shot = self.guild.add_invite("ONE", inviter=self.alice, max_uses=1)
        await self.tracker.sync_guild(self.guild)
        member = self.guild.add_member(user_id=500, age_days=30)
        self.guild.live.remove(one_shot)                   # Discord deletes it when used up
        att = await self.tracker.resolve_join(self.guild, member)
        self.assertEqual((att.status, att.inviter_id), ("single", 11))

    async def test_old_deleted_invite_is_not_a_candidate(self):
        one_shot = self.guild.add_invite("TWO", inviter=self.alice, max_uses=1)
        await self.tracker.sync_guild(self.guild)
        self.guild.live.remove(one_shot)
        await self.tracker.on_invite_deleted(one_shot)     # admin deleted it ...
        self.tracker._cache[1]["TWO"].deleted_at -= 3600   # ... an hour ago
        m, att, doc, awarded = await self.join(self.inv_b, age_days=30)
        self.assertEqual((att.status, att.inviter_id), ("single", 12))

    async def test_vanity_join_has_no_inviter(self):
        self.guild.features = ["VANITY_URL"]
        self.guild.vanity = discord.Invite(code="vanity", uses=0, inviter=None)
        await self.tracker.sync_guild(self.guild)
        member = self.guild.add_member(user_id=600, age_days=30)
        self.guild.vanity.uses += 1
        att = await self.tracker.resolve_join(self.guild, member)
        self.assertEqual(att.status, "vanity")
        doc, awarded = await self.ref.process_join(G, member, att, False)
        self.assertEqual(doc["validation_status"], "vanity_no_inviter")
        self.assertFalse(awarded)

    async def test_missing_permission_is_reported_not_crashed(self):
        self.guild.forbidden = True
        member = self.guild.add_member(user_id=700, age_days=30)
        att = await self.tracker.resolve_join(self.guild, member)
        self.assertEqual(att.status, "unavailable")
        doc, awarded = await self.ref.process_join(G, member, att, False)
        self.assertEqual(doc["validation_status"], "pending_review")
        self.assertIsNone(await self.tracker.sync_guild(self.guild))

    async def test_pending_referral_upgrades_when_attribution_becomes_clear(self):
        member = self.guild.add_member(user_id=800, age_days=30)
        att = await self.tracker.resolve_join(self.guild, member)          # nothing changed
        await self.ref.process_join(G, member, att, False)
        self.inv_a.uses += 1
        att2 = await self.tracker.resolve_join(self.guild, member)
        doc, awarded = await self.ref.process_join(G, member, att2, True)
        self.assertTrue(awarded)
        self.assertEqual(await self.balance(11), 3)

    async def test_inviter_who_left_goes_to_review(self):
        del self.guild.members[11]
        m, att, doc, awarded = await self.join(self.inv_a, age_days=30)
        self.assertEqual(doc["validation_status"], "pending_review")
        self.assertFalse(awarded)

    async def test_disabled_system_awards_nothing(self):
        await self.ref.update_config(G, {"enabled": False})
        m, att, doc, awarded = await self.join(self.inv_a, age_days=30)
        self.assertIsNone(doc)
        self.assertEqual(await self.balance(11), 0)

    async def test_snapshot_survives_restart(self):
        self.inv_a.uses = 5
        await self.tracker.sync_guild(self.guild)
        fresh = InviteTracker(self.db)                      # new process, empty memory
        member = self.guild.add_member(user_id=900, age_days=30)
        self.inv_a.uses += 1
        att = await fresh.resolve_join(self.guild, member)
        self.assertEqual((att.status, att.inviter_id), ("single", 11))

    async def test_personal_invite_reused_and_attributed_to_member(self):
        ch = self.guild.channel
        url1 = await self.tracker.get_or_create_personal_invite(self.guild, self.bob, ch)
        url2 = await self.tracker.get_or_create_personal_invite(self.guild, self.bob, ch)
        self.assertEqual(url1, url2)
        self.assertEqual(len([i for i in self.guild.live if i.inviter is self.guild.me]), 1)
        code = url1.rsplit("/", 1)[1]
        personal = next(i for i in self.guild.live if i.code == code)
        member = self.guild.add_member(user_id=950, age_days=30)
        personal.uses += 1
        att = await self.tracker.resolve_join(self.guild, member)
        self.assertEqual((att.status, att.inviter_id), ("single", 12))     # Bob, not the bot
        # link removed on Discord -> a new one is created instead of returning a dead link
        self.guild.live.remove(personal)
        url3 = await self.tracker.get_or_create_personal_invite(self.guild, self.bob, ch)
        self.assertNotEqual(url3, url1)

    async def test_concurrent_myinvite_creates_one_invite(self):
        ch = self.guild.channel
        urls = await asyncio.gather(*[self.tracker.get_or_create_personal_invite(self.guild, self.alice, ch)
                                      for _ in range(6)])
        self.assertEqual(len(set(urls)), 1)


class ReviewTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.inv_a = self.guild.add_invite("AAA", inviter=self.alice)
        self.inv_b = self.guild.add_invite("BBB", inviter=self.bob)
        await self.tracker.sync_guild(self.guild)

    async def _ambiguous(self, uid):
        m = self.guild.add_member(user_id=uid, age_days=30)
        self.inv_a.uses += 1
        self.inv_b.uses += 1
        att = await self.tracker.resolve_join(self.guild, m)
        await self.ref.process_join(G, m, att, True)
        return m

    async def test_admin_approves_and_points_paid_once(self):
        await self._ambiguous(1001)
        self.assertEqual(len(await self.ref.list_review(G)), 1)
        ok, text = await self.ref.review_decide(G, "1001", True, "11", "999")
        self.assertTrue(ok, text)
        ok2, _ = await self.ref.review_decide(G, "1001", True, "11", "999")
        self.assertFalse(ok2)
        self.assertEqual(await self.balance(11), 3)
        self.assertEqual(await self.ref.list_review(G), [])

    async def test_approve_requires_inviter_and_blocks_self(self):
        await self._ambiguous(1002)
        ok, _ = await self.ref.review_decide(G, "1002", True, None, "999")
        self.assertFalse(ok)
        ok, _ = await self.ref.review_decide(G, "1002", True, "1002", "999")
        self.assertFalse(ok)

    async def test_reject_pays_nothing(self):
        await self._ambiguous(1003)
        ok, _ = await self.ref.review_decide(G, "1003", False, None, "999")
        self.assertTrue(ok)
        self.assertEqual(await self.balance(11) + await self.balance(12), 0)
        self.assertEqual(await self.db.referral_audit_logs.count_documents({"action": "referral_rejected"}), 1)

    async def test_reconcile_finishes_interrupted_referral_reward(self):
        await self.ref.update_config(G, {"referral_points": 3})
        m = self.guild.add_member(user_id=1004, age_days=30)
        self.inv_a.uses += 1
        att = await self.tracker.resolve_join(self.guild, m)
        original = self.ref._award

        async def crash(*a, **k):
            raise RuntimeError("died before paying")
        self.ref._award = crash
        with self.assertRaises(RuntimeError):
            await self.ref.process_join(G, m, att, True)
        self.ref._award = original
        self.assertEqual(await self.balance(11), 0)
        self.assertEqual(await self.ref.reconcile(), 1)
        self.assertEqual(await self.ref.reconcile(), 0)
        self.assertEqual(await self.balance(11), 3)


class ChatRewardTests(Base):
    async def test_first_message_pays_one_point_only_once(self):
        self.assertTrue(await self.ref.claim_chat_reward(G, "50", {555}))
        for _ in range(5):
            self.assertFalse(await self.ref.claim_chat_reward(G, "50", {555}))
        self.assertEqual(await self.balance(50), 1)
        self.assertEqual((await self.points.get_user_stats(G, "50"))["chat_points"], 1)

    async def test_ineligible_channel_pays_nothing_and_does_not_consume_the_reward(self):
        self.assertFalse(await self.ref.claim_chat_reward(G, "51", {999}))
        self.assertEqual(await self.balance(51), 0)
        self.assertTrue(await self.ref.claim_chat_reward(G, "51", {555}))       # still available

    async def test_thread_parent_channel_counts(self):
        self.assertTrue(await self.ref.claim_chat_reward(G, "52", {12345, 555}))

    async def test_concurrent_messages_pay_once(self):
        res = await asyncio.gather(*[self.ref.claim_chat_reward(G, "53", {555}) for _ in range(10)])
        self.assertEqual(sum(res), 1)
        self.assertEqual(await self.balance(53), 1)

    async def test_survives_restart(self):
        await self.ref.claim_chat_reward(G, "54", {555})
        fresh = ReferralService(self.db, self.points)         # new process: empty in-memory cache
        self.assertFalse(await fresh.claim_chat_reward(G, "54", {555}))
        self.assertEqual(await self.balance(54), 1)

    async def test_interrupted_claim_is_completed_by_reconcile(self):
        original = self.ref._pay_chat

        async def crash(*a, **k):
            raise RuntimeError("died")
        self.ref._pay_chat = crash
        with self.assertRaises(RuntimeError):
            await self.ref.claim_chat_reward(G, "55", {555})
        self.ref._pay_chat = original
        self.assertEqual(await self.balance(55), 0)
        await self.ref.reconcile()
        await self.ref.reconcile()
        self.assertEqual(await self.balance(55), 1)

    async def test_disabled_or_zero_points(self):
        await self.ref.update_config(G, {"chat_points": 0})
        self.assertFalse(await self.ref.claim_chat_reward(G, "56", {555}))


class ConfigTests(Base):
    async def test_env_defaults_and_guild_overrides(self):
        from config import Config
        cfg = await self.ref.get_config("2")                  # a guild with no overrides
        self.assertEqual((cfg.referral_points, cfg.chat_points, cfg.min_account_age_days),
                         (Config.REFERRAL_POINTS, Config.CHAT_REWARD_POINTS, Config.MIN_ACCOUNT_AGE_DAYS))
        self.assertEqual((Config.REFERRAL_POINTS, Config.CHAT_REWARD_POINTS, Config.MIN_ACCOUNT_AGE_DAYS), (3, 1, 7))
        await self.ref.update_config("2", {"referral_points": 5}, add_to={"channel_ids": [7, 8]})
        cfg = await self.ref.get_config("2")
        self.assertEqual((cfg.referral_points, cfg.channel_ids), (5, {7, 8}))
        await self.ref.update_config("2", {}, remove_from={"channel_ids": [7]})
        self.assertEqual((await self.ref.get_config("2")).channel_ids, {8})

    def test_invalid_env_values_fall_back_with_a_warning(self):
        import os, importlib
        import config
        old = {k: os.environ.get(k) for k in ("REFERRAL_POINTS", "REFERRAL_CHANNEL_IDS", "MIN_ACCOUNT_AGE_DAYS")}
        os.environ.update({"REFERRAL_POINTS": "abc", "REFERRAL_CHANNEL_IDS": "1, x ,3", "MIN_ACCOUNT_AGE_DAYS": "-4"})
        try:
            config.CONFIG_WARNINGS.clear()
            importlib.reload(config)
            self.assertEqual(config.Config.REFERRAL_POINTS, 3)
            self.assertEqual(config.Config.REFERRAL_CHANNEL_IDS, [1, 3])
            self.assertEqual(config.Config.MIN_ACCOUNT_AGE_DAYS, 7)
            self.assertEqual(len(config.Config.validate_referral()), 3)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            config.CONFIG_WARNINGS.clear()
            importlib.reload(config)


if __name__ == "__main__":
    unittest.main()
