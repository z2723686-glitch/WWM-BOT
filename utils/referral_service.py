# -*- coding: utf-8 -*-
"""
WWM - Referral rules, first-chat reward, per-server reward settings, admin review.

Rules (defaults, overridable per server with /referral config):
  * 3 points per valid referral, 1 point for the first message in an eligible channel
  * invited account must be >= 7 days old, not a bot, not the inviter
  * ONE reward per invited account per server (unique index + idempotency key)
  * ambiguous / unattributed joins are never guessed: they wait for admin review

All point changes go through PointsService (idempotent ledger).
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional, Set

from pymongo import DESCENDING

from config import Config
from utils.invite_tracker import Attribution
from utils.points_service import PointsService
from utils.reward_common import DuplicateKeyError, as_utc, utcnow

logger = logging.getLogger("wwm.referral")

CONFIG_CACHE_SECONDS = 30

# validation_status values stored on `referrals`
V_VALID = "valid"
V_PENDING = "pending_review"
V_BOT = "invalid_bot"
V_SELF = "invalid_self"
V_AGE = "invalid_account_age"
V_VANITY = "vanity_no_inviter"
V_APPROVED = "approved_by_admin"
V_REJECTED = "rejected"


@dataclass
class RewardConfig:
    guild_id: str
    enabled: bool = True
    referral_points: int = 3
    chat_points: int = 1
    min_account_age_days: int = 7
    channel_ids: Set[int] = field(default_factory=set)
    announcement_channel_id: int = 0
    invite_channel_id: int = 0
    admin_role_ids: Set[int] = field(default_factory=set)
    default_campaign: str = ""
    panel_channel_id: int = 0
    panel_message_id: int = 0
    max_draw_entries: int = 10


class ReferralService:
    def __init__(self, db, points: PointsService):
        self.db = db
        self.points = points
        self._cfg_cache = {}
        self._claimed = set()          # (guild_id, user_id) already known to have claimed chat reward

    # ── Settings ──────────────────────────────────────────────────────────

    async def get_config(self, guild_id: str) -> RewardConfig:
        hit = self._cfg_cache.get(guild_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        doc = await self.db.referral_config.find_one({"guild_id": guild_id}) or {}

        def pick(key, default):
            value = doc.get(key)
            return default if value is None else value

        cfg = RewardConfig(
            guild_id=guild_id,
            enabled=bool(pick("enabled", Config.REFERRAL_ENABLED)),
            referral_points=int(pick("referral_points", Config.REFERRAL_POINTS)),
            chat_points=int(pick("chat_points", Config.CHAT_REWARD_POINTS)),
            min_account_age_days=int(pick("min_account_age_days", Config.MIN_ACCOUNT_AGE_DAYS)),
            channel_ids={int(c) for c in pick("channel_ids", Config.REFERRAL_CHANNEL_IDS)},
            announcement_channel_id=int(pick("announcement_channel_id", Config.REFERRAL_ANNOUNCEMENT_CHANNEL_ID)),
            invite_channel_id=int(pick("invite_channel_id", Config.REFERRAL_INVITE_CHANNEL_ID)),
            admin_role_ids={int(r) for r in (list(Config.REFERRAL_ADMIN_ROLE_IDS) + list(doc.get("admin_role_ids") or []))},
            default_campaign=str(pick("default_campaign", Config.REFERRAL_DEFAULT_CAMPAIGN) or ""),
            panel_channel_id=int(pick("panel_channel_id", 0)),
            panel_message_id=int(pick("panel_message_id", 0)),
            max_draw_entries=int(pick("max_draw_entries", Config.MAX_DRAW_ENTRIES_PER_REQUEST)),
        )
        self._cfg_cache[guild_id] = (time.monotonic() + CONFIG_CACHE_SECONDS, cfg)
        return cfg

    async def update_config(self, guild_id: str, updates: dict, *, add_to: Optional[dict] = None,
                            remove_from: Optional[dict] = None) -> None:
        ops = {}
        if updates:
            ops["$set"] = dict(updates, updated_at=utcnow())
        if add_to:
            ops["$addToSet"] = {k: {"$each": v} for k, v in add_to.items()}
        if remove_from:
            ops["$pull"] = {k: {"$in": v} for k, v in remove_from.items()}
        if ops:
            await self.db.referral_config.update_one({"guild_id": guild_id}, ops, upsert=True)
        self._cfg_cache.pop(guild_id, None)

    # ── Audit ─────────────────────────────────────────────────────────────

    async def audit(self, guild_id: str, actor_id: Optional[str], action: str, details: dict) -> None:
        try:
            await self.db.referral_audit_logs.insert_one({
                "guild_id": guild_id, "actor_id": actor_id, "action": action,
                "details": details, "created_at": utcnow()})
        except Exception:
            logger.exception("Audit log write failed (%s)", action)

    # ── Referrals ─────────────────────────────────────────────────────────

    async def process_join(self, guild_id: str, member, attribution: Attribution,
                           inviter_in_guild: bool = True) -> tuple:
        """
        Validate one join and award the inviter when everything checks out.
        Returns (referral_document, awarded_now). The document is None when the system
        is disabled or the member is a bot. Safe to call twice for the same member:
        the reward can only be paid once.
        """
        cfg = await self.get_config(guild_id)
        if not cfg.enabled or getattr(member, "bot", False):
            return None, False

        invited_id = str(member.id)
        existing = await self.db.referrals.find_one({"guild_id": guild_id, "invited_user_id": invited_id})
        if existing is not None:
            reopen = (existing.get("validation_status") == V_PENDING
                      and existing.get("reward_status") != "awarded"
                      and attribution.status == "single")
            if not reopen:
                await self.audit(guild_id, None, "rejoin_ignored",
                                 {"invited_user_id": invited_id, "status": existing.get("validation_status")})
                return existing, False

        status, reward = self._validate(cfg, member, attribution, inviter_in_guild)
        now = utcnow()
        doc = {
            "guild_id": guild_id,
            "inviter_id": str(attribution.inviter_id) if attribution.inviter_id else None,
            "invited_user_id": invited_id,
            "invite_code": attribution.code,
            "candidates": [{"code": c["code"],
                            "inviter_id": str(c["inviter_id"]) if c.get("inviter_id") else None}
                           for c in attribution.candidates],
            "account_created_at": as_utc(member.created_at).replace(tzinfo=None),
            "joined_at": now,
            "validation_status": status,
            "validation_reason": attribution.reason,
            "reward_status": "pending" if reward else "none",
            "points_awarded": 0,
            "transaction_id": None,
        }

        if existing is None:
            try:
                await self.db.referrals.insert_one(dict(doc))
            except DuplicateKeyError:        # two concurrent joins of the same user
                return await self.db.referrals.find_one(
                    {"guild_id": guild_id, "invited_user_id": invited_id}), False
        else:
            doc.pop("joined_at")
            await self.db.referrals.update_one(
                {"guild_id": guild_id, "invited_user_id": invited_id, "reward_status": {"$ne": "awarded"}},
                {"$set": doc})

        awarded_now = False
        if reward:
            awarded_now = await self._award(guild_id, invited_id, doc["inviter_id"], cfg.referral_points)
        return (await self.db.referrals.find_one(
            {"guild_id": guild_id, "invited_user_id": invited_id}), awarded_now)

    @staticmethod
    def _validate(cfg: RewardConfig, member, attribution: Attribution, inviter_in_guild: bool):
        """Returns (validation_status, should_reward)."""
        if getattr(member, "bot", False):
            return V_BOT, False
        if attribution.status == "vanity":
            return V_VANITY, False
        if attribution.status != "single" or attribution.inviter_id is None:
            return V_PENDING, False
        if attribution.inviter_id == member.id:
            return V_SELF, False
        age = utcnow() - as_utc(member.created_at).replace(tzinfo=None)
        if age < timedelta(days=cfg.min_account_age_days):
            return V_AGE, False
        if not inviter_in_guild:
            return V_PENDING, False
        return V_VALID, cfg.referral_points > 0

    async def _award(self, guild_id: str, invited_id: str, inviter_id: str, points: int) -> bool:
        """Pay the referral reward. True only for the call that actually credited the points."""
        result = await self.points.add_points(
            guild_id, inviter_id, points,
            txn_type="referral",
            description="Valid referral: <@{}> joined".format(invited_id),
            idempotency_key="referral:{}:{}".format(guild_id, invited_id),
            bucket="referral_points",
            extra_inc={"total_valid_referrals": 1},
            metadata={"invited_user_id": invited_id},
        )
        if result.ok:
            await self.db.referrals.update_one(
                {"guild_id": guild_id, "invited_user_id": invited_id},
                {"$set": {"reward_status": "awarded", "points_awarded": points,
                          "transaction_id": result.transaction_id, "awarded_at": utcnow()}})
        return bool(result.ok and not result.replayed)

    async def reconcile(self) -> int:
        """Finish rewards interrupted by a crash (pending referral rewards, unpaid chat claims)."""
        done = 0
        rows = await self.db.referrals.find(
            {"reward_status": "pending", "validation_status": {"$in": [V_VALID, V_APPROVED]}}
        ).to_list(length=500)
        for row in rows:
            cfg = await self.get_config(row["guild_id"])
            await self._award(row["guild_id"], row["invited_user_id"], row["inviter_id"],
                              row.get("points_awarded") or cfg.referral_points)
            done += 1
        claims = await self.db.chat_reward_claims.find({"transaction_id": None}).to_list(length=500)
        for claim in claims:
            await self._pay_chat(claim["guild_id"], claim["user_id"], claim.get("points") or 1)
            done += 1
        if done:
            logger.info("Reconciled %d interrupted reward(s)", done)
        return done

    # ── Admin review ──────────────────────────────────────────────────────

    async def list_review(self, guild_id: str, limit: int = 15) -> List[dict]:
        cur = self.db.referrals.find(
            {"guild_id": guild_id, "validation_status": V_PENDING, "reward_status": {"$ne": "awarded"}}
        ).sort("joined_at", DESCENDING).limit(limit)
        return await cur.to_list(length=limit)

    async def review_decide(self, guild_id: str, invited_id: str, approve: bool,
                            inviter_id: Optional[str], admin_id: str) -> tuple:
        """Returns (ok, message)."""
        doc = await self.db.referrals.find_one({"guild_id": guild_id, "invited_user_id": invited_id})
        if doc is None:
            return False, "No referral record exists for that member."
        if doc.get("reward_status") == "awarded":
            return False, "That referral was already rewarded."

        if not approve:
            await self.db.referrals.update_one(
                {"guild_id": guild_id, "invited_user_id": invited_id, "reward_status": {"$ne": "awarded"}},
                {"$set": {"validation_status": V_REJECTED, "reward_status": "none",
                          "reviewed_by": admin_id, "reviewed_at": utcnow()}})
            await self.audit(guild_id, admin_id, "referral_rejected", {"invited_user_id": invited_id})
            return True, "Referral rejected. No points were awarded."

        inviter_id = inviter_id or doc.get("inviter_id")
        if not inviter_id and len(doc.get("candidates") or []) == 1:
            inviter_id = doc["candidates"][0].get("inviter_id")
        if not inviter_id:
            return False, "Choose who invited this member (the `inviter` option)."
        if inviter_id == invited_id:
            return False, "A member cannot refer themselves."

        cfg = await self.get_config(guild_id)
        await self.db.referrals.update_one(
            {"guild_id": guild_id, "invited_user_id": invited_id, "reward_status": {"$ne": "awarded"}},
            {"$set": {"inviter_id": inviter_id, "validation_status": V_APPROVED,
                      "reward_status": "pending", "reviewed_by": admin_id, "reviewed_at": utcnow()}})
        await self._award(guild_id, invited_id, inviter_id, cfg.referral_points)
        await self.audit(guild_id, admin_id, "referral_approved",
                         {"invited_user_id": invited_id, "inviter_id": inviter_id})
        return True, "Approved: <@{}> received **{}** point(s).".format(inviter_id, cfg.referral_points)

    # ── Stats ─────────────────────────────────────────────────────────────

    async def user_summary(self, guild_id: str, user_id: str) -> dict:
        wallet = await self.points.get_user_stats(guild_id, user_id)
        pending = await self.db.referrals.count_documents(
            {"guild_id": guild_id, "validation_status": V_PENDING, "reward_status": {"$ne": "awarded"},
             "inviter_id": user_id})
        return {"valid": wallet["total_valid_referrals"], "pending": pending,
                "points": wallet["referral_points"]}

    async def guild_stats(self, guild_id: str) -> dict:
        out = {}
        for key, flt in (
            ("rewarded", {"reward_status": "awarded"}),
            ("pending_review", {"validation_status": V_PENDING, "reward_status": {"$ne": "awarded"}}),
            ("invalid_age", {"validation_status": V_AGE}),
            ("invalid_self", {"validation_status": V_SELF}),
            ("rejected", {"validation_status": V_REJECTED}),
            ("total", {}),
        ):
            out[key] = await self.db.referrals.count_documents(dict(flt, guild_id=guild_id))
        out["chat_claims"] = await self.db.chat_reward_claims.count_documents({"guild_id": guild_id})
        wallets = await self.db.referral_users.find({"guild_id": guild_id}).to_list(length=100000)
        out["points_in_circulation"] = sum(int(w.get("points_balance", 0)) for w in wallets)
        out["points_issued"] = sum(int(w.get("total_points_earned", 0)) for w in wallets)
        out["points_spent"] = sum(int(w.get("total_points_spent", 0)) for w in wallets)
        return out

    # ── First-chat reward ─────────────────────────────────────────────────

    async def claim_chat_reward(self, guild_id: str, user_id: str, channel_ids) -> bool:
        """
        Award the one-time chat reward. Returns True only for the call that actually
        created the claim. Repeated calls (and restarts) never pay twice: a unique
        index on (guild_id, user_id) guards the claim and an idempotency key the points.
        """
        key = (guild_id, user_id)
        if key in self._claimed:
            return False
        cfg = await self.get_config(guild_id)
        eligible = {int(c) for c in channel_ids if c} & cfg.channel_ids
        if not cfg.enabled or cfg.chat_points <= 0 or not eligible:
            return False
        channel_id = sorted(eligible)[0]

        try:
            await self.db.chat_reward_claims.insert_one({
                "guild_id": guild_id, "user_id": user_id, "channel_id": str(channel_id),
                "points": cfg.chat_points, "transaction_id": None, "created_at": utcnow()})
        except DuplicateKeyError:
            self._claimed.add(key)
            return False

        self._claimed.add(key)
        await self._pay_chat(guild_id, user_id, cfg.chat_points)
        return True

    async def _pay_chat(self, guild_id: str, user_id: str, points: int) -> None:
        result = await self.points.add_points(
            guild_id, user_id, points, txn_type="chat",
            description="First message in an eligible channel",
            idempotency_key="chat:{}:{}".format(guild_id, user_id),
            bucket="chat_points")
        if result.ok:
            await self.db.chat_reward_claims.update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {"$set": {"transaction_id": result.transaction_id}})
