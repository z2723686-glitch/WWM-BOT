# -*- coding: utf-8 -*-
"""
WWM - Points wallet (reward points, NOT XP).

Design (safe on a standalone MongoDB, no multi-document transactions needed):

* Every change is first written to the immutable ledger `reward_transactions`
  with a UNIQUE idempotency_key and status "pending".
* The balance change is ONE atomic single-document update on the wallet
  (`referral_users`). That same update also records the transaction id in the
  wallet's `applied_txns` marker list and (for deductions) is conditional on
  `points_balance >= amount`, so balances can never go negative and a
  transaction can never be applied twice.
* The ledger row is then marked "applied". A crash between the two steps leaves
  a "pending" row that `recover_pending()` completes safely at the next start.
* Replaying the same idempotency_key returns the original result.

Balances are integers. UI callbacks never touch balances directly; they call
this service.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from pymongo import DESCENDING, ReturnDocument

from utils.reward_common import DuplicateKeyError, long_id, utcnow

logger = logging.getLogger("wwm.points")

APPLIED_MARKERS_KEPT = 100


@dataclass
class TxnResult:
    ok: bool
    transaction_id: Optional[str] = None
    balance_after: Optional[int] = None
    replayed: bool = False
    reason: Optional[str] = None      # e.g. "insufficient_funds"

    @property
    def insufficient(self) -> bool:
        return self.reason == "insufficient_funds"


class PointsService:
    def __init__(self, db):
        self.db = db   # the bot's Database manager (self.bot.db)

    # ── Wallet ────────────────────────────────────────────────────────────

    @staticmethod
    def _wallet_defaults(guild_id: str, user_id: str) -> dict:
        now = utcnow()
        return {
            "guild_id": guild_id, "user_id": user_id,
            "points_balance": 0, "referral_points": 0, "chat_points": 0,
            "total_points_earned": 0, "total_points_spent": 0,
            "total_valid_referrals": 0, "total_draw_entries": 0,
            "applied_txns": [],
            "created_at": now, "updated_at": now,
        }

    async def ensure_wallet(self, guild_id: str, user_id: str) -> None:
        try:
            await self.db.referral_users.update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {"$setOnInsert": self._wallet_defaults(guild_id, user_id)},
                upsert=True,
            )
        except DuplicateKeyError:
            pass  # concurrent creation - wallet exists, which is all we need

    async def get_wallet(self, guild_id: str, user_id: str) -> dict:
        doc = await self.db.referral_users.find_one({"guild_id": guild_id, "user_id": user_id})
        return doc or self._wallet_defaults(guild_id, user_id)

    async def get_balance(self, guild_id: str, user_id: str) -> int:
        return int((await self.get_wallet(guild_id, user_id)).get("points_balance", 0))

    async def get_user_stats(self, guild_id: str, user_id: str) -> dict:
        w = await self.get_wallet(guild_id, user_id)
        return {k: int(w.get(k, 0)) for k in (
            "points_balance", "referral_points", "chat_points",
            "total_points_earned", "total_points_spent",
            "total_valid_referrals", "total_draw_entries")}

    async def get_transaction_history(self, guild_id: str, user_id: str, limit: int = 10) -> list:
        cur = self.db.reward_transactions.find(
            {"guild_id": guild_id, "user_id": user_id, "status": "applied"}
        ).sort("created_at", DESCENDING).limit(limit)
        return await cur.to_list(length=limit)

    async def get_leaderboard(self, guild_id: str, limit: int = 10,
                              by: str = "total_points_earned") -> list:
        if by not in ("total_points_earned", "points_balance", "total_valid_referrals"):
            by = "total_points_earned"
        cur = self.db.referral_users.find(
            {"guild_id": guild_id, by: {"$gt": 0}},
            {"user_id": 1, "points_balance": 1, "total_points_earned": 1, "total_valid_referrals": 1},
        ).sort(by, DESCENDING).limit(limit)
        return await cur.to_list(length=limit)

    # ── Public mutations ──────────────────────────────────────────────────

    async def add_points(self, guild_id: str, user_id: str, amount: int, *,
                         txn_type: str, description: str, idempotency_key: str,
                         bucket: Optional[str] = None, extra_inc: Optional[dict] = None,
                         metadata: Optional[dict] = None) -> TxnResult:
        """
        Credit points exactly once per idempotency_key.
        bucket: "referral_points" | "chat_points" | None (extra per-source counter).
        extra_inc: additional wallet counters incremented in the SAME atomic update
                   (e.g. {"total_valid_referrals": 1}).
        """
        amount = int(amount)
        if amount <= 0:
            raise ValueError("add_points amount must be positive")
        return await self._submit(guild_id, user_id, amount, txn_type, description,
                                  idempotency_key, bucket, extra_inc, metadata)

    async def deduct_points(self, guild_id: str, user_id: str, amount: int, *,
                            txn_type: str, description: str, idempotency_key: str,
                            extra_inc: Optional[dict] = None,
                            metadata: Optional[dict] = None) -> TxnResult:
        """Debit points exactly once; never lets the balance go below zero."""
        amount = int(amount)
        if amount <= 0:
            raise ValueError("deduct_points amount must be positive")
        return await self._submit(guild_id, user_id, -amount, txn_type, description,
                                  idempotency_key, None, extra_inc, metadata)

    # ── Internals ─────────────────────────────────────────────────────────

    async def _submit(self, guild_id, user_id, amount, txn_type, description,
                      idempotency_key, bucket, extra_inc, metadata) -> TxnResult:
        txn = {
            "transaction_id": long_id(),
            "guild_id": guild_id, "user_id": user_id,
            "transaction_type": txn_type,
            "amount": amount,
            "balance_after": None,
            "description": description,
            "idempotency_key": idempotency_key,
            "status": "pending",
            "bucket": bucket,
            "extra_inc": extra_inc or {},
            "metadata": metadata or {},
            "created_at": utcnow(),
            "applied_at": None,
        }
        try:
            await self.db.reward_transactions.insert_one(txn)
        except DuplicateKeyError:
            existing = await self.db.reward_transactions.find_one({"idempotency_key": idempotency_key})
            if existing is None:      # extremely unlikely (deleted between calls)
                raise
            return await self._resume(existing)
        return await self._apply(txn)

    async def _resume(self, existing: dict) -> TxnResult:
        status = existing.get("status")
        if status == "applied":
            return TxnResult(True, existing["transaction_id"], existing.get("balance_after"), replayed=True)
        if status == "failed":
            return TxnResult(False, existing["transaction_id"], None, replayed=True,
                             reason=existing.get("failure_reason"))
        result = await self._apply(existing)      # pending: finish it (idempotent)
        result.replayed = True
        return result

    async def _apply(self, txn: dict) -> TxnResult:
        gid, uid, tid, amount = txn["guild_id"], txn["user_id"], txn["transaction_id"], txn["amount"]
        await self.ensure_wallet(gid, uid)

        inc = {"points_balance": amount}
        if amount > 0 and str(txn.get("transaction_type", "")).endswith("refund"):
            inc["total_points_spent"] = -amount      # a refund reverses spending, it is not new income
        elif amount > 0:
            inc["total_points_earned"] = amount
            if txn.get("bucket") in ("referral_points", "chat_points"):
                inc[txn["bucket"]] = amount
        else:
            inc["total_points_spent"] = -amount
        for key, value in (txn.get("extra_inc") or {}).items():
            inc[key] = inc.get(key, 0) + value

        flt = {"guild_id": gid, "user_id": uid, "applied_txns": {"$ne": tid}}
        if amount < 0:
            flt["points_balance"] = {"$gte": -amount}

        wallet = await self.db.referral_users.find_one_and_update(
            flt,
            {"$inc": inc,
             "$set": {"updated_at": utcnow()},
             "$push": {"applied_txns": {"$each": [tid], "$slice": -APPLIED_MARKERS_KEPT}}},
            return_document=ReturnDocument.AFTER,
        )

        if wallet is not None:
            balance = int(wallet["points_balance"])
            await self._mark(tid, "applied", balance_after=balance)
            return TxnResult(True, tid, balance)

        # The conditional update did not match: already applied, or insufficient funds
        current = await self.db.referral_users.find_one({"guild_id": gid, "user_id": uid}) or {}
        if tid in (current.get("applied_txns") or []):
            await self._mark(tid, "applied", balance_after=None, recovered=True)
            return TxnResult(True, tid, int(current.get("points_balance", 0)), replayed=True)

        await self._mark(tid, "failed", failure_reason="insufficient_funds")
        return TxnResult(False, tid, int(current.get("points_balance", 0)), reason="insufficient_funds")

    async def _mark(self, tid: str, status: str, **fields) -> None:
        update = {"status": status, "applied_at": utcnow()}
        update.update(fields)
        await self.db.reward_transactions.update_one(
            {"transaction_id": tid, "status": "pending"}, {"$set": update})

    async def recover_pending(self, older_than_seconds: int = 20) -> int:
        """Complete ledger rows left 'pending' by a crash. Returns how many were handled."""
        cutoff = utcnow() - timedelta(seconds=older_than_seconds)
        cursor = self.db.reward_transactions.find({"status": "pending", "created_at": {"$lte": cutoff}})
        rows = await cursor.to_list(length=500)
        for row in rows:
            try:
                await self._apply(row)
            except Exception:
                logger.exception("Could not recover transaction %s", row.get("transaction_id"))
        if rows:
            logger.info("Recovered %d pending point transaction(s)", len(rows))
        return len(rows)
