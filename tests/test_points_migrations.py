import asyncio
import unittest

import bootstrap  # noqa: F401  (must be first)
from fake_mongo import FakeDatabase
from utils.points_service import PointsService
from utils.reward_common import utcnow
from utils.reward_migrations import MIGRATIONS, run_pending_migrations
from datetime import timedelta

G, U = "1", "100"


class PointsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = FakeDatabase()
        await run_pending_migrations(self.db)
        self.pts = PointsService(self.db)

    async def test_credit_is_idempotent(self):
        a = await self.pts.add_points(G, U, 3, txn_type="referral", description="x", idempotency_key="k1")
        b = await self.pts.add_points(G, U, 3, txn_type="referral", description="x", idempotency_key="k1")
        self.assertTrue(a.ok and b.ok and b.replayed)
        self.assertEqual(await self.pts.get_balance(G, U), 3)
        self.assertEqual(await self.db.reward_transactions.count_documents({}), 1)

    async def test_insufficient_funds_rejected_and_balance_untouched(self):
        await self.pts.add_points(G, U, 2, txn_type="chat", description="x", idempotency_key="a")
        res = await self.pts.deduct_points(G, U, 5, txn_type="draw_spend", description="x", idempotency_key="b")
        self.assertFalse(res.ok)
        self.assertTrue(res.insufficient)
        self.assertEqual(await self.pts.get_balance(G, U), 2)

    async def test_concurrent_deductions_never_go_negative(self):
        await self.pts.add_points(G, U, 5, txn_type="chat", description="x", idempotency_key="seed")
        results = await asyncio.gather(*[
            self.pts.deduct_points(G, U, 1, txn_type="draw_spend", description="x", idempotency_key="d%d" % i)
            for i in range(20)])
        self.assertEqual(sum(1 for r in results if r.ok), 5)
        self.assertEqual(await self.pts.get_balance(G, U), 0)

    async def test_crash_after_wallet_update_does_not_double_apply(self):
        original = self.pts._mark
        calls = {"n": 0}

        async def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("process died before ledger was marked")
            return await original(*a, **kw)

        self.pts._mark = flaky
        with self.assertRaises(RuntimeError):
            await self.pts.add_points(G, U, 4, txn_type="referral", description="x", idempotency_key="crash")
        self.assertEqual(await self.pts.get_balance(G, U), 4)           # wallet already updated
        row = await self.db.reward_transactions.find_one({"idempotency_key": "crash"})
        self.assertEqual(row["status"], "pending")

        self.pts._mark = original
        again = await self.pts.add_points(G, U, 4, txn_type="referral", description="x", idempotency_key="crash")
        self.assertTrue(again.ok)
        self.assertEqual(await self.pts.get_balance(G, U), 4)           # still 4, not 8
        row = await self.db.reward_transactions.find_one({"idempotency_key": "crash"})
        self.assertEqual(row["status"], "applied")

    async def test_recover_pending_applies_once(self):
        # a ledger row written but never applied (crash right after insert)
        await self.db.reward_transactions.insert_one({
            "transaction_id": "t1", "guild_id": G, "user_id": U, "transaction_type": "referral",
            "amount": 3, "balance_after": None, "description": "x", "idempotency_key": "orphan",
            "status": "pending", "bucket": "referral_points", "extra_inc": {}, "metadata": {},
            "created_at": utcnow() - timedelta(minutes=5), "applied_at": None})
        self.assertEqual(await self.pts.recover_pending(), 1)
        self.assertEqual(await self.pts.recover_pending(), 0)
        self.assertEqual(await self.pts.get_balance(G, U), 3)

    async def test_stats_leaderboard_history(self):
        await self.pts.add_points(G, U, 3, txn_type="referral", description="r", idempotency_key="r1",
                                  bucket="referral_points", extra_inc={"total_valid_referrals": 1})
        await self.pts.add_points(G, U, 1, txn_type="chat", description="c", idempotency_key="c1", bucket="chat_points")
        await self.pts.deduct_points(G, U, 2, txn_type="draw_spend", description="d", idempotency_key="d1",
                                     extra_inc={"total_draw_entries": 2})
        s = await self.pts.get_user_stats(G, U)
        self.assertEqual(s, {"points_balance": 2, "referral_points": 3, "chat_points": 1,
                             "total_points_earned": 4, "total_points_spent": 2,
                             "total_valid_referrals": 1, "total_draw_entries": 2})
        self.assertEqual(len(await self.pts.get_transaction_history(G, U)), 3)
        board = await self.pts.get_leaderboard(G)
        self.assertEqual(board[0]["user_id"], U)

    async def test_refund_reverses_spending_not_earnings(self):
        await self.pts.add_points(G, U, 5, txn_type="chat", description="x", idempotency_key="s")
        await self.pts.deduct_points(G, U, 2, txn_type="draw_spend", description="x", idempotency_key="d")
        await self.pts.add_points(G, U, 2, txn_type="draw_refund", description="x", idempotency_key="rf")
        s = await self.pts.get_user_stats(G, U)
        self.assertEqual((s["points_balance"], s["total_points_earned"], s["total_points_spent"]), (5, 5, 0))


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_once_and_is_repeatable(self):
        db = FakeDatabase()
        db.users.docs.append({"guild_id": "1", "user_id": "9", "xp": 1234})        # existing data
        db.bound_accounts.docs.append({"discord_id": "9", "uid": "55"})
        first = await run_pending_migrations(db)
        self.assertEqual(len(first["applied"]), len(MIGRATIONS))
        second = await run_pending_migrations(db)
        self.assertEqual(second["applied"], [])
        self.assertEqual(len(second["skipped"]), len(MIGRATIONS))
        self.assertEqual(db.users.docs[0]["xp"], 1234)                              # untouched
        self.assertEqual(db.bound_accounts.docs[0]["uid"], "55")
        names = {n for n in await db.list_collection_names()}
        for needed in ("referral_users", "referral_invites", "referrals", "reward_transactions",
                       "chat_reward_claims", "points_draws", "points_draw_entries", "points_prizes",
                       "points_prize_winners", "referral_audit_logs", "schema_migrations", "referral_config"):
            self.assertIn(needed, names)
        self.assertEqual(await db.schema_migrations.count_documents({"status": "applied"}), len(MIGRATIONS))

    async def test_failed_migration_is_recorded_and_retried(self):
        db = FakeDatabase()
        import utils.reward_migrations as rm
        original = rm.MIGRATIONS[0].run

        async def boom(_db):
            raise RuntimeError("disk full")

        failing = rm.Migration(rm.MIGRATIONS[0].version, "x", boom)
        saved = list(rm.MIGRATIONS)
        rm.MIGRATIONS[0] = failing
        try:
            res = await run_pending_migrations(db)
            self.assertEqual(res["failed"], [failing.version])
            self.assertEqual(res["applied"], [])
        finally:
            rm.MIGRATIONS[:] = saved
        res = await run_pending_migrations(db)          # next start: retried and succeeds
        self.assertEqual(res["failed"], [])
        self.assertEqual(len(res["applied"]), len(MIGRATIONS))

    async def test_index_names_unique_per_collection(self):
        import utils.reward_migrations as rm
        for spec in (rm._V1_INDEXES, rm._V2_INDEXES):
            for coll, models in spec.items():
                names = [m.document["name"] for m in models]
                self.assertEqual(len(names), len(set(names)), coll)


if __name__ == "__main__":
    unittest.main()


class DatabaseHookTests(unittest.IsolatedAsyncioTestCase):
    """The hooks added to database.py (Database.connect() calls them after _create_indexes)."""

    async def _manager(self, hello):
        from database import Database
        mgr = Database()
        mgr.db = FakeDatabase()
        admin = type("A", (), {})()

        async def command(name):
            if isinstance(hello, Exception):
                raise hello
            return hello
        admin.command = command
        mgr.client = type("C", (), {"admin": admin})()
        return mgr

    async def test_migrations_hook_sets_ready_flag_and_is_repeatable(self):
        mgr = await self._manager({})
        self.assertFalse(mgr.rewards_ready)
        await mgr._run_reward_migrations()
        self.assertTrue(mgr.rewards_ready)
        await mgr._run_reward_migrations()                       # second start: nothing pending
        self.assertTrue(mgr.rewards_ready)

    async def test_failed_migration_does_not_raise_and_blocks_only_the_reward_cog(self):
        mgr = await self._manager({})

        async def broken(_):
            raise RuntimeError("no permission to create indexes")
        import utils.reward_migrations as rm
        saved = list(rm.MIGRATIONS)
        rm.MIGRATIONS[0] = rm.Migration(saved[0].version, "x", broken)
        try:
            await mgr._run_reward_migrations()                   # must not raise (bot keeps running)
        finally:
            rm.MIGRATIONS[:] = saved
        self.assertFalse(mgr.rewards_ready)

    async def test_transaction_detection(self):
        for hello, expected in (({"setName": "rs0"}, True), ({"msg": "isdbgrid"}, True),
                                ({"isWritablePrimary": True}, False), (RuntimeError("denied"), False)):
            mgr = await self._manager(hello)
            await mgr._detect_transactions()
            self.assertEqual(mgr.supports_transactions, expected, hello)

    async def test_collection_handles_are_wired(self):
        mgr = await self._manager({})
        from database import Database
        src = open(Database.__init__.__code__.co_filename, encoding="utf-8").read()
        for name in ("referral_users", "referral_invites", "referrals", "reward_transactions",
                     "chat_reward_claims", "points_draws", "points_draw_entries", "points_prizes",
                     "points_prize_winners", "referral_audit_logs", "schema_migrations", "referral_config"):
            self.assertIn('self.{0}'.format(name), src)
            self.assertIn('self.db["{0}"]'.format(name), src)
