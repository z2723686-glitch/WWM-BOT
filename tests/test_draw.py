import asyncio
import unittest
from datetime import timedelta

import bootstrap  # noqa: F401
from fake_mongo import FakeDatabase
from utils.lucky_draw_service import LuckyDrawService, weighted_pick
from utils.points_service import PointsService
from utils.reward_common import utcnow
from utils.reward_migrations import run_pending_migrations

G = "1"


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = FakeDatabase()
        await run_pending_migrations(self.db)
        self.points = PointsService(self.db)
        self.draws = LuckyDrawService(self.db, self.points)

    async def fund(self, uid, amount):
        await self.points.add_points(G, str(uid), amount, txn_type="admin_add", description="seed",
                                     idempotency_key="seed:%s" % uid)

    async def bal(self, uid):
        return await self.points.get_balance(G, str(uid))

    async def prize(self, name, stock, weight, **kw):
        return await self.draws.add_prize(G, name, stock=stock, weight=weight, **kw)

    async def stock(self, prize):
        return (await self.db.points_prizes.find_one({"prize_id": prize["prize_id"]}))["stock"]


class InstantDrawTests(Base):
    async def test_deducts_exactly_the_entries_used(self):
        await self.prize("Ticket", 100, 1)
        await self.prize("Nothing", 0, 1, is_losing=True)
        await self.fund(1, 10)
        out = await self.draws.instant_draw(G, "1", "req-1", 4)
        self.assertTrue(out.ok)
        self.assertEqual((len(out.results), out.cost, out.balance_after), (4, 4, 6))
        self.assertEqual(await self.bal(1), 6)
        self.assertEqual((await self.points.get_user_stats(G, "1"))["total_draw_entries"], 4)

    async def test_insufficient_balance_rejected_without_drawing(self):
        p = await self.prize("Ticket", 5, 1)
        await self.prize("Nothing", 0, 1, is_losing=True)
        await self.fund(2, 2)
        out = await self.draws.instant_draw(G, "2", "req-2", 3)
        self.assertFalse(out.ok)
        self.assertEqual(out.error, "insufficient_funds")
        self.assertEqual(await self.bal(2), 2)
        self.assertEqual(await self.stock(p), 5)
        self.assertEqual(await self.db.points_prize_winners.count_documents({}), 0)

    async def test_user_with_zero_points_rejected(self):
        await self.prize("Nothing", 0, 1, is_losing=True)
        out = await self.draws.instant_draw(G, "3", "req-3", 1)
        self.assertEqual(out.error, "insufficient_funds")

    async def test_repeated_interaction_returns_original_result_and_charges_once(self):
        await self.prize("A", 50, 1)
        await self.prize("B", 50, 1)
        await self.fund(4, 5)
        first = await self.draws.instant_draw(G, "4", "same-interaction", 3)
        again = [await self.draws.instant_draw(G, "4", "same-interaction", 3) for _ in range(3)]
        self.assertTrue(all(a.replayed and a.results == first.results for a in again))
        self.assertEqual(await self.bal(4), 2)                       # charged once
        self.assertEqual(await self.db.points_draw_entries.count_documents({}), 1)

    async def test_same_interaction_in_parallel_draws_once(self):
        await self.prize("A", 50, 1)
        await self.prize("B", 50, 1)
        await self.fund(5, 5)
        outs = await asyncio.gather(*[self.draws.instant_draw(G, "5", "dup", 2) for _ in range(6)])
        self.assertEqual(await self.bal(5), 3)
        self.assertEqual(len({tuple(r["prize_id"] for r in o.results) for o in outs}), 1)

    async def test_failed_discord_reply_cannot_trigger_a_new_draw(self):
        """The result is saved before any reply; retrying the same request reads it back."""
        await self.prize("A", 9, 1)
        await self.fund(6, 3)
        out = await self.draws.instant_draw(G, "6", "int-9", 1)
        saved = await self.db.points_draw_entries.find_one({"request_key": out.request_key})
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(len(saved["results"]), 1)
        retry = await self.draws.instant_draw(G, "6", "int-9", 1)
        self.assertEqual(retry.results, out.results)
        self.assertEqual(await self.bal(6), 2)

    async def test_stock_never_negative_and_last_unit_goes_to_one_member(self):
        p = await self.prize("Last one", 1, 10)
        for uid in range(1, 31):
            await self.fund(uid, 1)
        outs = await asyncio.gather(*[self.draws.instant_draw(G, str(u), "r%d" % u, 1) for u in range(1, 31)])
        winners = [o for o in outs if o.ok and any(not r["is_losing"] for r in o.results)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(await self.stock(p), 0)
        self.assertEqual(await self.db.points_prize_winners.count_documents({}), 1)
        # members who found the pool empty were refunded in full, nobody lost a point for nothing
        for o, uid in zip(outs, range(1, 31)):
            if o.ok and not o.results:
                self.assertEqual(await self.bal(uid), 1)

    async def test_concurrent_winners_never_exceed_inventory(self):
        p = await self.prize("Limited", 5, 100)
        await self.prize("Nothing", 0, 1, is_losing=True)
        for uid in range(1, 41):
            await self.fund(uid, 2)
        await asyncio.gather(*[self.draws.instant_draw(G, str(u), "x%d" % u, 2) for u in range(1, 41)])
        won = await self.db.points_prize_winners.count_documents({})
        self.assertLessEqual(won, 5)
        self.assertGreaterEqual(await self.stock(p), 0)
        self.assertEqual(won + await self.stock(p), 5)                # every unit accounted for

    async def test_no_prizes_available_is_rejected_before_charging(self):
        await self.fund(7, 3)
        out = await self.draws.instant_draw(G, "7", "r", 1)
        self.assertEqual(out.error, "no_prizes")
        self.assertEqual(await self.bal(7), 3)

    async def test_pool_running_dry_midway_refunds_unplayed_entries(self):
        await self.prize("Only two", 2, 5)
        await self.fund(8, 5)
        out = await self.draws.instant_draw(G, "8", "r8", 5)
        self.assertTrue(out.ok)
        self.assertEqual(len(out.results), 2)
        self.assertEqual(out.refunded, 3)
        self.assertEqual(await self.bal(8), 3)
        s = await self.points.get_user_stats(G, "8")
        self.assertEqual((s["total_points_spent"], s["total_draw_entries"]), (2, 2))

    async def test_losing_outcome_creates_no_winner_record(self):
        await self.prize("Nothing", 0, 1, is_losing=True)
        await self.fund(9, 3)
        out = await self.draws.instant_draw(G, "9", "r9", 3)
        self.assertTrue(all(r["is_losing"] for r in out.results))
        self.assertEqual(await self.db.points_prize_winners.count_documents({}), 0)

    async def test_winner_record_created_with_pending_delivery(self):
        await self.prize("Ticket", 3, 10)
        await self.fund(10, 1)
        out = await self.draws.instant_draw(G, "10", "r10", 1)
        w = await self.db.points_prize_winners.find_one({})
        self.assertEqual((w["user_id"], w["prize_name"], w["status"]), ("10", "Ticket", "pending"))
        self.assertEqual(await self.draws.claim_pending(G, "10"), 1)
        self.assertEqual(await self.draws.claim_pending(G, "10"), 0)
        row = await self.draws.set_delivery(G, w["winner_id"], "delivered", "999", "sent in game")
        self.assertEqual((row["status"], row["delivered_by"]), ("delivered", "999"))

    async def test_entry_limits(self):
        await self.prize("A", 5, 1)
        await self.fund(11, 100)
        self.assertEqual((await self.draws.instant_draw(G, "11", "a", 0)).error, "invalid_entries")
        self.assertEqual((await self.draws.instant_draw(G, "11", "b", 11, max_entries=10)).error, "invalid_entries")

    async def test_odds_follow_admin_weights_and_zero_weight_never_wins(self):
        await self.prize("Common", 0, 3, is_losing=True)
        await self.prize("Rare", 0, 1, is_losing=True)
        await self.prize("Never", 0, 0, is_losing=True)
        pool = await self.draws.get_pool(G, "default")
        self.assertEqual(len(pool), 2)                                # zero-weight prize is not in the pool
        picks = {"Common": 0, "Rare": 0}
        for _ in range(8000):
            picks[weighted_pick(pool)["name"]] += 1
        share = picks["Rare"] / 8000                                  # expected 25%
        self.assertTrue(0.21 < share < 0.29, share)

    async def test_crash_mid_request_is_resumed_without_redrawing(self):
        await self.prize("A", 100, 1)
        await self.prize("B", 100, 1)
        await self.fund(12, 5)
        original = self.draws._spin_once
        calls = {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("process died on spin 3")
            return await original(*a, **k)

        self.draws._spin_once = flaky
        with self.assertRaises(RuntimeError):
            await self.draws.instant_draw(G, "12", "crash", 4)
        row = await self.db.points_draw_entries.find_one({"request_key": "instant:1:12:crash"})
        self.assertEqual((row["status"], row["spins_done"]), ("pending", 2))
        kept = list(row["results"])
        self.assertEqual(await self.bal(12), 1)                        # charged exactly once

        self.draws._spin_once = original
        await self.db.points_draw_entries.update_one(
            {"request_key": "instant:1:12:crash"}, {"$set": {"created_at": utcnow() - timedelta(minutes=5)}})
        self.assertEqual(await self.draws.resume_unfinished(), 1)
        row = await self.db.points_draw_entries.find_one({"request_key": "instant:1:12:crash"})
        self.assertEqual((row["status"], len(row["results"])), ("completed", 4))
        self.assertEqual(row["results"][:2], kept)                     # earlier spins untouched
        self.assertEqual(await self.bal(12), 1)
        self.assertEqual(await self.db.points_prize_winners.count_documents({}), 4)

    async def test_prize_admin_functions(self):
        p = await self.prize("Ticket", 5, 10, category="Ticket")
        self.assertEqual(p["category"], "ticket")
        e = await self.draws.edit_prize(G, p["prize_id"], {"stock": 9, "weight": 4, "active": True})
        self.assertEqual((e["stock"], e["weight"]), (9, 4))
        self.assertTrue(await self.draws.remove_prize(G, p["prize_id"]))
        self.assertFalse(await self.draws.remove_prize(G, p["prize_id"]))
        self.assertEqual(await self.draws.list_prizes(G), [])
        self.assertIsNone(await self.draws.edit_prize(G, "nope", {"stock": 1}))
        neg = await self.prize("Neg", -5, -1)
        self.assertEqual((neg["stock"], neg["weight"]), (0, 0))

    async def test_state_survives_restart(self):
        await self.prize("A", 10, 1)
        await self.fund(13, 3)
        out = await self.draws.instant_draw(G, "13", "r13", 2)
        fresh_points = PointsService(self.db)
        fresh = LuckyDrawService(self.db, fresh_points)               # simulated restart
        self.assertEqual(await fresh_points.get_balance(G, "13"), 1)
        again = await fresh.instant_draw(G, "13", "r13", 2)
        self.assertTrue(again.replayed)
        self.assertEqual(again.results, out.results)
        self.assertEqual(len(await fresh.get_history(G, "13")), 1)


class RaffleTests(Base):
    async def make(self, cap=0):
        d = await self.draws.create_draw(G, "Weekend", "raffle", max_entries_per_user=cap)
        self.assertTrue(await self.draws.start_draw(G, d["draw_id"]))
        return d

    async def test_entries_cost_points_and_cap_is_enforced(self):
        d = await self.make(cap=5)
        await self.fund(1, 20)
        a = await self.draws.enter_raffle(G, "1", d["draw_id"], "i1", 3)
        b = await self.draws.enter_raffle(G, "1", d["draw_id"], "i2", 3)
        self.assertTrue(a.ok)
        self.assertEqual(b.error, "cap_reached")
        self.assertEqual(await self.bal(1), 17)
        self.assertEqual((await self.draws.enter_raffle(G, "1", d["draw_id"], "i1", 3)).replayed, True)
        self.assertEqual(await self.bal(1), 17)

    async def test_concurrent_entries_by_one_member_respect_cap(self):
        d = await self.make(cap=5)
        await self.fund(2, 50)
        outs = await asyncio.gather(*[self.draws.enter_raffle(G, "2", d["draw_id"], "c%d" % i, 2) for i in range(6)])
        self.assertEqual(sum(1 for o in outs if o.ok), 2)
        self.assertEqual(await self.bal(2), 46)

    async def test_insufficient_balance_rejected(self):
        d = await self.make()
        await self.fund(3, 1)
        out = await self.draws.enter_raffle(G, "3", d["draw_id"], "z", 2)
        self.assertEqual(out.error, "insufficient_funds")
        self.assertEqual(await self.bal(3), 1)

    async def test_draft_or_closed_campaign_not_enterable(self):
        d = await self.draws.create_draw(G, "Later", "raffle")
        await self.fund(4, 5)
        self.assertEqual((await self.draws.enter_raffle(G, "4", d["draw_id"], "q", 1)).error, "campaign_closed")

    async def test_close_draws_unique_winners_within_stock_and_only_once(self):
        d = await self.make()
        p = await self.draws.add_prize(G, "Ticket", stock=3, weight=1, campaign_id=d["draw_id"])
        for uid in range(1, 9):
            await self.fund(uid, 10)
            await self.draws.enter_raffle(G, str(uid), d["draw_id"], "e%d" % uid, uid)    # uid tickets each
        winners = await self.draws.close_raffle(G, d["draw_id"])
        self.assertEqual(len(winners), 3)
        self.assertEqual(len({w["user_id"] for w in winners}), 3)
        self.assertEqual(await self.stock(p), 0)
        self.assertIsNone(await self.draws.close_raffle(G, d["draw_id"]))                 # second close refused
        self.assertEqual((await self.draws.get_draw(G, d["draw_id"]))["status"], "completed")
        res = await self.draws.draw_results(G, d["draw_id"])
        self.assertEqual((res["participants"], res["entries"]), (8, 36))

    async def test_more_prizes_than_participants(self):
        d = await self.make()
        p = await self.draws.add_prize(G, "Ticket", stock=5, weight=1, campaign_id=d["draw_id"])
        await self.fund(1, 5)
        await self.draws.enter_raffle(G, "1", d["draw_id"], "only", 1)
        winners = await self.draws.close_raffle(G, d["draw_id"])
        self.assertEqual(len(winners), 1)
        self.assertEqual(await self.stock(p), 4)                       # unused stock is not consumed

    async def test_interrupted_close_resumes_without_duplicate_winners(self):
        d = await self.make()
        await self.draws.add_prize(G, "Ticket", stock=2, weight=1, campaign_id=d["draw_id"])
        for uid in (1, 2, 3):
            await self.fund(uid, 2)
            await self.draws.enter_raffle(G, str(uid), d["draw_id"], "e%d" % uid, 1)
        original = self.draws._insert_winner
        calls = {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("died after first winner")
            return await original(*a, **k)

        self.draws._insert_winner = flaky
        with self.assertRaises(RuntimeError):
            await self.draws.close_raffle(G, d["draw_id"])
        self.draws._insert_winner = original
        self.assertEqual((await self.draws.get_draw(G, d["draw_id"]))["status"], "closing")
        await self.draws.resume_unfinished()
        winners = await self.db.points_prize_winners.find({"draw_id": d["draw_id"]}).to_list(length=10)
        self.assertLessEqual(len(winners), 2)
        self.assertEqual(len({w["user_id"] for w in winners}), len(winners))
        self.assertEqual((await self.draws.get_draw(G, d["draw_id"]))["status"], "completed")

    async def test_cancel_refunds_everyone_once(self):
        d = await self.make()
        for uid in (1, 2):
            await self.fund(uid, 10)
            await self.draws.enter_raffle(G, str(uid), d["draw_id"], "e%d" % uid, 4)
        self.assertEqual(await self.draws.cancel_draw(G, d["draw_id"]), 8)
        self.assertIsNone(await self.draws.cancel_draw(G, d["draw_id"]))
        self.assertEqual((await self.bal(1), await self.bal(2)), (10, 10))
        s = await self.points.get_user_stats(G, "1")
        self.assertEqual((s["total_points_spent"], s["total_draw_entries"]), (0, 0))

    async def test_cannot_cancel_after_winners_drawn(self):
        d = await self.make()
        await self.draws.close_raffle(G, d["draw_id"])
        self.assertIsNone(await self.draws.cancel_draw(G, d["draw_id"]))


if __name__ == "__main__":
    unittest.main()
