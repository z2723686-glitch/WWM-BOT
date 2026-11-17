# -*- coding: utf-8 -*-
"""
WWM - Lucky draw (points -> entries). Fully independent from the account-binding
giveaway in cogs/binding.py (separate collections, separate commands).

1 point = 1 draw entry.

Two modes
  * Instant Draw : every entry is one spin of the prize pool, result is immediate.
  * Campaign Raffle: entries are tickets; when an admin closes the campaign winners
    are drawn (without replacement, one prize per member, weighted by tickets).

Safety guarantees
  * Winning odds are never hard-coded: they come from admin-set prize weights and
    the stock that is actually available. "Losing" outcomes are ordinary prizes
    flagged is_losing (unlimited stock) with their own weight.
  * Randomness uses `secrets` (OS CSPRNG).
  * One draw request = one document with a UNIQUE request_key (built from the
    Discord interaction id). Replays return the stored result; they never draw again.
  * Points are deducted through the idempotent ledger BEFORE any spin; every spin
    result is persisted before the Discord reply is sent.
  * Stock is decremented with a conditional atomic update (`stock > 0`), so stock
    can never go negative and the last unit can only go to one member.
  * Crash safety: a half-finished request is resumed on restart (spins already
    persisted are never redrawn). In the worst case a single stock unit stays
    reserved without a winner - it is never given out twice.
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional

from pymongo import DESCENDING

from utils.points_service import PointsService
from utils.reward_common import DuplicateKeyError, long_id, short_id, utcnow

logger = logging.getLogger("wwm.draw")

ENTRY_COST = 1                 # points per entry
DEFAULT_CAMPAIGN = "default"
STOCK_RETRIES = 25
STALE_SECONDS = 20


@dataclass
class DrawOutcome:
    ok: bool
    error: Optional[str] = None            # insufficient_funds | no_prizes | campaign_closed | invalid_entries | cap_reached | busy
    replayed: bool = False
    results: List[dict] = field(default_factory=list)
    entries: int = 0
    cost: int = 0
    refunded: int = 0
    balance_after: Optional[int] = None
    request_key: Optional[str] = None


def weighted_pick(pool: List[dict]) -> Optional[dict]:
    """Cryptographically secure weighted choice. Weights are admin-configured integers."""
    weights = [max(0, int(p.get("weight", 0))) for p in pool]
    total = sum(weights)
    if total <= 0:
        return None
    ticket = secrets.randbelow(total)
    running = 0
    for prize, weight in zip(pool, weights):
        running += weight
        if ticket < running:
            return prize
    return None


class LuckyDrawService:
    def __init__(self, db, points: PointsService):
        self.db = db
        self.points = points
        self._locks = {}

    def _lock(self, key) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    # ── Prizes ────────────────────────────────────────────────────────────

    async def add_prize(self, guild_id: str, name: str, *, stock: int, weight: int,
                        category: str = "general", is_losing: bool = False,
                        campaign_id: str = DEFAULT_CAMPAIGN, description: str = "",
                        actor_id: Optional[str] = None) -> dict:
        now = utcnow()
        doc = {
            "prize_id": short_id(), "guild_id": guild_id, "campaign_id": campaign_id or DEFAULT_CAMPAIGN,
            "name": name.strip()[:80], "category": (category or "general").strip()[:40].lower(),
            "description": description.strip()[:200],
            "weight": max(0, int(weight)), "is_losing": bool(is_losing),
            "unlimited": bool(is_losing),                 # losing outcomes never run out
            "stock": 0 if is_losing else max(0, int(stock)),
            "initial_stock": 0 if is_losing else max(0, int(stock)),
            "active": True, "removed": False,
            "created_by": actor_id, "created_at": now, "updated_at": now,
        }
        await self.db.points_prizes.insert_one(dict(doc))
        return doc

    async def get_prize(self, guild_id: str, prize_id: str) -> Optional[dict]:
        return await self.db.points_prizes.find_one(
            {"guild_id": guild_id, "prize_id": prize_id, "removed": {"$ne": True}})

    async def edit_prize(self, guild_id: str, prize_id: str, fields: dict) -> Optional[dict]:
        allowed = {}
        if "name" in fields and fields["name"]:
            allowed["name"] = fields["name"].strip()[:80]
        if "category" in fields and fields["category"]:
            allowed["category"] = fields["category"].strip()[:40].lower()
        if "description" in fields and fields["description"] is not None:
            allowed["description"] = fields["description"].strip()[:200]
        if "weight" in fields and fields["weight"] is not None:
            allowed["weight"] = max(0, int(fields["weight"]))
        if "stock" in fields and fields["stock"] is not None:
            allowed["stock"] = max(0, int(fields["stock"]))
        if "active" in fields and fields["active"] is not None:
            allowed["active"] = bool(fields["active"])
        if not allowed:
            return await self.get_prize(guild_id, prize_id)
        allowed["updated_at"] = utcnow()
        return await self.db.points_prizes.find_one_and_update(
            {"guild_id": guild_id, "prize_id": prize_id, "removed": {"$ne": True}},
            {"$set": allowed}, return_document=True)

    async def remove_prize(self, guild_id: str, prize_id: str) -> bool:
        res = await self.db.points_prizes.update_one(
            {"guild_id": guild_id, "prize_id": prize_id, "removed": {"$ne": True}},
            {"$set": {"removed": True, "active": False, "updated_at": utcnow()}})
        return res.modified_count == 1

    async def list_prizes(self, guild_id: str, campaign_id: Optional[str] = None) -> List[dict]:
        flt = {"guild_id": guild_id, "removed": {"$ne": True}}
        if campaign_id:
            flt["campaign_id"] = campaign_id
        return await self.db.points_prizes.find(flt).sort("name", 1).to_list(length=500)

    async def get_pool(self, guild_id: str, campaign_id: str) -> List[dict]:
        rows = await self.db.points_prizes.find(
            {"guild_id": guild_id, "campaign_id": campaign_id, "active": True,
             "removed": {"$ne": True}, "weight": {"$gt": 0}}).to_list(length=500)
        return [r for r in rows if r.get("unlimited") or int(r.get("stock", 0)) > 0]

    async def prize_pool_summary(self, guild_id: str, campaign_id: str = DEFAULT_CAMPAIGN) -> List[dict]:
        """Real (non-losing) prizes that are still available - for the panel."""
        rows = await self.list_prizes(guild_id, campaign_id)
        return [r for r in rows if r.get("active") and not r.get("is_losing") and int(r.get("stock", 0)) > 0]

    # ── Campaigns ─────────────────────────────────────────────────────────

    async def create_draw(self, guild_id: str, name: str, draw_type: str, *,
                          max_entries_per_user: int = 0, actor_id: Optional[str] = None) -> dict:
        doc = {
            "draw_id": short_id(), "guild_id": guild_id, "name": name.strip()[:80],
            "type": draw_type, "status": "draft",
            "entry_cost": ENTRY_COST, "max_entries_per_user": max(0, int(max_entries_per_user)),
            "created_by": actor_id, "created_at": utcnow(),
            "opened_at": None, "closed_at": None, "winners_count": 0,
        }
        await self.db.points_draws.insert_one(dict(doc))
        return doc

    async def get_draw(self, guild_id: str, draw_id: str) -> Optional[dict]:
        return await self.db.points_draws.find_one({"guild_id": guild_id, "draw_id": draw_id})

    async def list_draws(self, guild_id: str, statuses=None) -> List[dict]:
        flt = {"guild_id": guild_id}
        if statuses:
            flt["status"] = {"$in": list(statuses)}
        return await self.db.points_draws.find(flt).sort("created_at", DESCENDING).to_list(length=50)

    async def start_draw(self, guild_id: str, draw_id: str) -> bool:
        res = await self.db.points_draws.update_one(
            {"guild_id": guild_id, "draw_id": draw_id, "status": "draft"},
            {"$set": {"status": "open", "opened_at": utcnow()}})
        return res.modified_count == 1

    async def resolve_campaign(self, guild_id: str, ref: Optional[str]) -> Optional[dict]:
        """None/'default' -> the built-in instant pool. Otherwise an open campaign doc."""
        if not ref or ref == DEFAULT_CAMPAIGN:
            return {"draw_id": DEFAULT_CAMPAIGN, "type": "instant", "status": "open",
                    "name": "Lucky Draw", "max_entries_per_user": 0}
        return await self.get_draw(guild_id, ref)

    # ── Instant draw ──────────────────────────────────────────────────────

    async def instant_draw(self, guild_id: str, user_id: str, request_id: str,
                           entries: int, campaign_ref: Optional[str] = None,
                           max_entries: int = 10) -> DrawOutcome:
        entries = int(entries)
        if entries < 1 or entries > max_entries:
            return DrawOutcome(False, "invalid_entries")
        campaign = await self.resolve_campaign(guild_id, campaign_ref)
        if campaign is None or campaign.get("type") != "instant" or campaign.get("status") != "open":
            return DrawOutcome(False, "campaign_closed")
        campaign_id = campaign["draw_id"]
        key = "instant:{}:{}:{}".format(guild_id, user_id, request_id)

        existing = await self.db.points_draw_entries.find_one({"request_key": key})
        if existing is None:
            if not await self.get_pool(guild_id, campaign_id):
                return DrawOutcome(False, "no_prizes")
            doc = {
                "request_key": key, "guild_id": guild_id, "user_id": user_id,
                "draw_id": campaign_id, "campaign_name": campaign.get("name"), "mode": "instant",
                "entries": entries, "cost": entries * ENTRY_COST,
                "status": "pending", "spins_done": 0, "results": [], "refunded": 0,
                "created_at": utcnow(), "completed_at": None,
            }
            try:
                await self.db.points_draw_entries.insert_one(dict(doc))
            except DuplicateKeyError:
                pass       # same interaction raced us - fall through and read it

        async with self._lock(key):
            doc = await self.db.points_draw_entries.find_one({"request_key": key})
            return await self._process_instant(doc)

    async def _process_instant(self, doc: dict) -> DrawOutcome:
        key, gid, uid = doc["request_key"], doc["guild_id"], doc["user_id"]

        def outcome(d, replayed=False, error=None):
            return DrawOutcome(
                ok=(d.get("status") == "completed"), error=error or d.get("failure_reason"),
                replayed=replayed, results=list(d.get("results") or []), entries=d["entries"],
                cost=d["cost"], refunded=int(d.get("refunded", 0)), request_key=key)

        if doc["status"] in ("completed", "failed"):
            out = outcome(doc, replayed=True)
            out.balance_after = await self.points.get_balance(gid, uid)
            return out

        spend = await self.points.deduct_points(
            gid, uid, doc["cost"], txn_type="draw_spend",
            description="Lucky draw x{}".format(doc["entries"]),
            idempotency_key="draw-spend:" + key,
            extra_inc={"total_draw_entries": doc["entries"]},
            metadata={"request_key": key})
        if not spend.ok:
            await self.db.points_draw_entries.update_one(
                {"request_key": key, "status": "pending"},
                {"$set": {"status": "failed", "failure_reason": spend.reason or "insufficient_funds",
                          "completed_at": utcnow()}})
            fresh = await self.db.points_draw_entries.find_one({"request_key": key})
            out = outcome(fresh, error=spend.reason or "insufficient_funds")
            out.balance_after = spend.balance_after
            return out

        spins_done = int(doc.get("spins_done", 0))
        refunded = 0
        for spin in range(spins_done, doc["entries"]):
            result = await self._spin_once(gid, doc["draw_id"], spin)
            if result is None:                       # pool exhausted mid-request
                remaining = doc["entries"] - spin
                refund = await self.points.add_points(
                    gid, uid, remaining * ENTRY_COST, txn_type="draw_refund",
                    description="Refund: prize pool ran out",
                    idempotency_key="draw-refund:" + key,
                    extra_inc={"total_draw_entries": -remaining})
                refunded = remaining * ENTRY_COST if refund.ok else 0
                break
            res = await self.db.points_draw_entries.update_one(
                {"request_key": key, "spins_done": spin},
                {"$push": {"results": result}, "$inc": {"spins_done": 1}})
            if res.modified_count != 1:
                logger.warning("Spin %d of %s was already recorded - stopping", spin, key)
                break

        fresh = await self.db.points_draw_entries.find_one({"request_key": key})
        await self._create_winners(fresh)
        await self.db.points_draw_entries.update_one(
            {"request_key": key, "status": "pending"},
            {"$set": {"status": "completed", "refunded": refunded, "completed_at": utcnow()}})
        fresh = await self.db.points_draw_entries.find_one({"request_key": key})
        out = outcome(fresh)
        out.balance_after = await self.points.get_balance(gid, uid)
        return out

    async def _spin_once(self, guild_id: str, campaign_id: str, spin: int) -> Optional[dict]:
        """Pick a prize and reserve one unit of stock. None if nothing can be won."""
        for _ in range(STOCK_RETRIES):
            pool = await self.get_pool(guild_id, campaign_id)
            prize = weighted_pick(pool)
            if prize is None:
                return None
            if not prize.get("unlimited"):
                res = await self.db.points_prizes.update_one(
                    {"prize_id": prize["prize_id"], "stock": {"$gt": 0}, "active": True},
                    {"$inc": {"stock": -1}, "$set": {"updated_at": utcnow()}})
                if res.modified_count != 1:
                    continue                          # someone took the last unit - choose again
            return {"spin": spin, "prize_id": prize["prize_id"], "name": prize["name"],
                    "category": prize.get("category", "general"), "is_losing": bool(prize.get("is_losing"))}
        return None

    async def _create_winners(self, entry_doc: dict) -> None:
        for r in entry_doc.get("results") or []:
            if r.get("is_losing"):
                continue
            await self._insert_winner(entry_doc["guild_id"], entry_doc["user_id"], r,
                                      entry_doc["request_key"], entry_doc["draw_id"], r["spin"])

    async def _insert_winner(self, guild_id, user_id, prize_ref: dict, request_key, draw_id, spin_index):
        try:
            await self.db.points_prize_winners.insert_one({
                "winner_id": long_id()[:10], "guild_id": guild_id, "user_id": user_id,
                "prize_id": prize_ref["prize_id"], "prize_name": prize_ref["name"],
                "category": prize_ref.get("category", "general"),
                "draw_id": draw_id, "request_key": request_key, "spin_index": spin_index,
                "status": "pending", "created_at": utcnow(),
                "claimed_at": None, "delivered_at": None, "delivered_by": None, "note": ""})
        except DuplicateKeyError:
            pass       # already recorded (replay / resume)

    # ── Raffle ────────────────────────────────────────────────────────────

    async def enter_raffle(self, guild_id: str, user_id: str, draw_id: str,
                           request_id: str, entries: int, max_entries: int = 100) -> DrawOutcome:
        entries = int(entries)
        if entries < 1 or entries > max_entries:
            return DrawOutcome(False, "invalid_entries")
        draw = await self.get_draw(guild_id, draw_id)
        if draw is None or draw.get("type") != "raffle" or draw.get("status") != "open":
            return DrawOutcome(False, "campaign_closed")
        key = "raffle:{}:{}:{}".format(draw_id, user_id, request_id)

        async with self._lock(("raffle", draw_id, user_id)):
            existing = await self.db.points_draw_entries.find_one({"request_key": key})
            if existing is not None:
                return await self._finish_raffle_entry(existing, replayed=True)

            cap = int(draw.get("max_entries_per_user", 0))
            if cap:
                rows = await self.db.points_draw_entries.find(
                    {"draw_id": draw_id, "user_id": user_id, "mode": "raffle",
                     "status": {"$in": ["pending", "confirmed"]}}).to_list(length=1000)
                if sum(int(r["entries"]) for r in rows) + entries > cap:
                    return DrawOutcome(False, "cap_reached")

            doc = {"request_key": key, "guild_id": guild_id, "user_id": user_id, "draw_id": draw_id,
                   "campaign_name": draw.get("name"), "mode": "raffle", "entries": entries,
                   "cost": entries * ENTRY_COST, "status": "pending", "results": [],
                   "created_at": utcnow(), "completed_at": None}
            try:
                await self.db.points_draw_entries.insert_one(dict(doc))
            except DuplicateKeyError:
                doc = await self.db.points_draw_entries.find_one({"request_key": key})
            return await self._finish_raffle_entry(doc)

    async def _finish_raffle_entry(self, doc: dict, replayed: bool = False) -> DrawOutcome:
        key, gid, uid = doc["request_key"], doc["guild_id"], doc["user_id"]
        if doc["status"] == "pending":
            spend = await self.points.deduct_points(
                gid, uid, doc["cost"], txn_type="raffle_spend",
                description="Raffle entries x{} ({})".format(doc["entries"], doc.get("campaign_name")),
                idempotency_key="raffle-spend:" + key,
                extra_inc={"total_draw_entries": doc["entries"]}, metadata={"request_key": key})
            if spend.ok:
                await self.db.points_draw_entries.update_one(
                    {"request_key": key, "status": "pending"},
                    {"$set": {"status": "confirmed", "completed_at": utcnow()}})
            else:
                await self.db.points_draw_entries.update_one(
                    {"request_key": key, "status": "pending"},
                    {"$set": {"status": "failed", "failure_reason": "insufficient_funds",
                              "completed_at": utcnow()}})
            doc = await self.db.points_draw_entries.find_one({"request_key": key})
        ok = doc["status"] == "confirmed"
        return DrawOutcome(ok, None if ok else doc.get("failure_reason", "failed"), replayed=replayed,
                           entries=doc["entries"], cost=doc["cost"], request_key=key,
                           balance_after=await self.points.get_balance(gid, uid))

    async def close_raffle(self, guild_id: str, draw_id: str) -> Optional[List[dict]]:
        """Stop entries and draw winners. Returns the winner docs, or None if not closable."""
        res = await self.db.points_draws.update_one(
            {"guild_id": guild_id, "draw_id": draw_id, "type": "raffle", "status": "open"},
            {"$set": {"status": "closing"}})
        if res.modified_count != 1:
            return None
        return await self._finish_closing(guild_id, draw_id)

    async def _finish_closing(self, guild_id: str, draw_id: str) -> List[dict]:
        key = "raffle-win:{}".format(draw_id)
        existing = await self.db.points_prize_winners.find({"request_key": key}).to_list(length=1000)
        won_users = {w["user_id"] for w in existing}
        spin_index = len(existing)

        rows = await self.db.points_draw_entries.find(
            {"draw_id": draw_id, "mode": "raffle", "status": "confirmed"}).to_list(length=100000)
        tickets = {}
        for r in rows:
            tickets[r["user_id"]] = tickets.get(r["user_id"], 0) + int(r["entries"])

        prizes = [p for p in await self.list_prizes(guild_id, draw_id)
                  if p.get("active") and not p.get("is_losing") and int(p.get("stock", 0)) > 0]
        for prize in prizes:
            while True:
                candidates = {u: n for u, n in tickets.items() if u not in won_users and n > 0}
                if not candidates:
                    break
                res = await self.db.points_prizes.update_one(
                    {"prize_id": prize["prize_id"], "stock": {"$gt": 0}},
                    {"$inc": {"stock": -1}, "$set": {"updated_at": utcnow()}})
                if res.modified_count != 1:
                    break                                   # this prize is out of stock
                users = list(candidates)
                total = sum(candidates.values())
                ticket = secrets.randbelow(total)
                running, winner = 0, users[-1]
                for u in users:
                    running += candidates[u]
                    if ticket < running:
                        winner = u
                        break
                await self._insert_winner(guild_id, winner, prize, key, draw_id, spin_index)
                won_users.add(winner)
                spin_index += 1

        await self.db.points_draws.update_one(
            {"guild_id": guild_id, "draw_id": draw_id, "status": "closing"},
            {"$set": {"status": "completed", "closed_at": utcnow(), "winners_count": spin_index}})
        return await self.db.points_prize_winners.find({"request_key": key}).to_list(length=1000)

    async def cancel_draw(self, guild_id: str, draw_id: str) -> Optional[int]:
        """Cancel a draft/open campaign; raffle entries are refunded. Returns refunded points or None."""
        res = await self.db.points_draws.update_one(
            {"guild_id": guild_id, "draw_id": draw_id, "status": {"$in": ["draft", "open"]}},
            {"$set": {"status": "cancelled", "closed_at": utcnow()}})
        if res.modified_count != 1:
            return None
        refunded = 0
        rows = await self.db.points_draw_entries.find(
            {"draw_id": draw_id, "mode": "raffle", "status": "confirmed"}).to_list(length=100000)
        for r in rows:
            out = await self.points.add_points(
                guild_id, r["user_id"], r["cost"], txn_type="raffle_refund",
                description="Refund: campaign cancelled",
                idempotency_key="raffle-refund:" + r["request_key"],
                extra_inc={"total_draw_entries": -int(r["entries"])})
            if out.ok:
                refunded += r["cost"]
                await self.db.points_draw_entries.update_one(
                    {"request_key": r["request_key"]}, {"$set": {"status": "refunded"}})
        return refunded

    async def draw_results(self, guild_id: str, draw_id: str) -> dict:
        draw = await self.get_draw(guild_id, draw_id)
        if draw is None:
            return {}
        winners = await self.db.points_prize_winners.find(
            {"guild_id": guild_id, "draw_id": draw_id}).sort("created_at", 1).to_list(length=500)
        rows = await self.db.points_draw_entries.find(
            {"draw_id": draw_id, "status": {"$in": ["confirmed", "completed"]}}).to_list(length=100000)
        return {"draw": draw, "winners": winners,
                "participants": len({r["user_id"] for r in rows}),
                "entries": sum(int(r["entries"]) for r in rows)}

    # ── History & delivery ────────────────────────────────────────────────

    async def get_history(self, guild_id: str, user_id: str, limit: int = 8) -> List[dict]:
        return await self.db.points_draw_entries.find(
            {"guild_id": guild_id, "user_id": user_id, "status": {"$in": ["completed", "confirmed", "refunded"]}}
        ).sort("created_at", DESCENDING).limit(limit).to_list(length=limit)

    async def get_wins(self, guild_id: str, user_id: str, limit: int = 15) -> List[dict]:
        return await self.db.points_prize_winners.find(
            {"guild_id": guild_id, "user_id": user_id}
        ).sort("created_at", DESCENDING).limit(limit).to_list(length=limit)

    async def claim_pending(self, guild_id: str, user_id: str) -> int:
        res = await self.db.points_prize_winners.update_many(
            {"guild_id": guild_id, "user_id": user_id, "status": "pending"},
            {"$set": {"status": "claimed", "claimed_at": utcnow()}})
        return res.modified_count

    async def set_delivery(self, guild_id: str, winner_id: str, status: str,
                           admin_id: str, note: str = "") -> Optional[dict]:
        update = {"status": status, "note": note[:200]}
        if status == "delivered":
            update.update({"delivered_at": utcnow(), "delivered_by": admin_id})
        elif status == "claimed":
            update["claimed_at"] = utcnow()
        return await self.db.points_prize_winners.find_one_and_update(
            {"guild_id": guild_id, "winner_id": winner_id}, {"$set": update}, return_document=True)

    async def list_winners(self, guild_id: str, status: Optional[str] = None, limit: int = 20) -> List[dict]:
        flt = {"guild_id": guild_id}
        if status:
            flt["status"] = status
        return await self.db.points_prize_winners.find(flt).sort("created_at", DESCENDING).limit(limit).to_list(length=limit)

    # ── Restart recovery ──────────────────────────────────────────────────

    async def resume_unfinished(self) -> int:
        """Finish draw requests / raffle closings interrupted by a restart."""
        cutoff = utcnow() - timedelta(seconds=STALE_SECONDS)
        count = 0
        rows = await self.db.points_draw_entries.find(
            {"status": "pending", "created_at": {"$lte": cutoff}}).to_list(length=500)
        for row in rows:
            try:
                async with self._lock(row["request_key"]):
                    fresh = await self.db.points_draw_entries.find_one({"request_key": row["request_key"]})
                    if fresh["mode"] == "instant":
                        await self._process_instant(fresh)
                    else:
                        await self._finish_raffle_entry(fresh)
                count += 1
            except Exception:
                logger.exception("Could not resume draw request %s", row.get("request_key"))
        closing = await self.db.points_draws.find({"status": "closing"}).to_list(length=50)
        for draw in closing:
            try:
                await self._finish_closing(draw["guild_id"], draw["draw_id"])
                count += 1
            except Exception:
                logger.exception("Could not finish closing draw %s", draw.get("draw_id"))
        if count:
            logger.info("Resumed %d unfinished draw operation(s)", count)
        return count
