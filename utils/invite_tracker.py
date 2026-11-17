# -*- coding: utf-8 -*-
"""
WWM - Invite tracking (who invited a new member?).

How it works (discord.py has no "which invite was used" event, so we diff usage):
  1. Snapshot every guild invite (code -> uses, max_uses, inviter) at startup and
     keep the snapshot in memory AND in MongoDB (`referral_invites`).
  2. On member join (under a per-guild asyncio lock) fetch the invites again and
     compare. Exactly ONE invite whose use-count went up  =>  that is the inviter.
  3. Anything else is NEVER guessed. The caller gets an Attribution with
     status "ambiguous" / "none" / "unavailable" and the referral goes to admin review.

Special cases handled: invites that disappeared because their last use was
consumed, vanity URLs (no inviter), several joins arriving at once through the
same invite (short-lived carry credit), restarts (DB snapshot), missing
"Manage Server" permission (status "unavailable").

Invites created by the bot for /myinvite are owned by the Discord user they were
made for (`owner_id`), not by the bot account that created them.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import discord

from utils.reward_common import as_utc, utcnow

logger = logging.getLogger("wwm.invites")

VANITY_KEY = "__vanity__"
RECENT_DELETE_SECONDS = 20     # an exhausted invite deleted this recently may be the one just used
CARRY_SECONDS = 10             # leftover credit when 2+ joins used the same invite at once


@dataclass
class InviteSnap:
    code: str
    uses: int = 0
    max_uses: int = 0
    inviter_id: Optional[int] = None     # who Discord says created it
    owner_id: Optional[int] = None       # who it is attributed to (set for bot-made /myinvite links)
    expires_at: Optional[object] = None  # naive UTC datetime or None
    deleted_at: Optional[float] = None   # epoch seconds when we saw INVITE_DELETE

    @property
    def credit_user(self) -> Optional[int]:
        return self.owner_id or self.inviter_id


@dataclass
class Attribution:
    status: str                       # single | ambiguous | none | vanity | unavailable
    code: Optional[str] = None
    inviter_id: Optional[int] = None
    candidates: List[dict] = field(default_factory=list)   # [{"code":..., "inviter_id":...}]
    reason: str = ""


class InviteTracker:
    def __init__(self, db):
        self.db = db
        self._cache: Dict[int, Dict[str, InviteSnap]] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        self._user_locks: Dict[tuple, asyncio.Lock] = {}
        self._carry: Dict[int, Dict[str, tuple]] = {}
        self._warned_forbidden = set()

    # ── helpers ───────────────────────────────────────────────────────────

    def lock_for(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = self._locks[guild_id] = asyncio.Lock()
        return lock

    @staticmethod
    def _snap_from_invite(inv, owner_id: Optional[int]) -> InviteSnap:
        exp = getattr(inv, "expires_at", None)
        exp = as_utc(exp).replace(tzinfo=None) if exp else None
        inviter = getattr(inv, "inviter", None)
        return InviteSnap(
            code=inv.code,
            uses=int(getattr(inv, "uses", 0) or 0),
            max_uses=int(getattr(inv, "max_uses", 0) or 0),
            inviter_id=inviter.id if inviter else None,
            owner_id=owner_id,
            expires_at=exp,
        )

    @staticmethod
    def _snap_from_doc(doc: dict) -> InviteSnap:
        return InviteSnap(
            code=doc["code"], uses=int(doc.get("uses", 0)), max_uses=int(doc.get("max_uses", 0)),
            inviter_id=int(doc["inviter_id"]) if doc.get("inviter_id") else None,
            owner_id=int(doc["owner_id"]) if doc.get("owner_id") else None,
            expires_at=doc.get("expires_at"),
        )

    async def _fetch_current(self, guild, prev: Dict[str, InviteSnap]) -> Dict[str, InviteSnap]:
        """Live invites from Discord. Raises discord.Forbidden / HTTPException."""
        out: Dict[str, InviteSnap] = {}
        for inv in await guild.invites():
            old = prev.get(inv.code)
            owner = old.owner_id if old else None
            if old is None:      # unseen code: it may be a /myinvite link whose event we haven't got yet
                doc = await self.db.referral_invites.find_one({"guild_id": str(guild.id), "code": inv.code})
                owner = int(doc["owner_id"]) if doc and doc.get("owner_id") else None
            out[inv.code] = self._snap_from_invite(inv, owner)

        features = getattr(guild, "features", []) or []
        if "VANITY_URL" in features:
            try:
                vanity = await guild.vanity_invite()
                if vanity is not None:
                    out[VANITY_KEY] = InviteSnap(code=VANITY_KEY, uses=int(vanity.uses or 0))
            except (discord.Forbidden, discord.HTTPException):
                pass
        return out

    async def _load_cache(self, guild_id: int) -> Dict[str, InviteSnap]:
        cached = self._cache.get(guild_id)
        if cached is not None:
            return cached
        snaps: Dict[str, InviteSnap] = {}
        cursor = self.db.referral_invites.find({"guild_id": str(guild_id), "deleted": {"$ne": True}})
        for doc in await cursor.to_list(length=2000):
            snaps[doc["code"]] = self._snap_from_doc(doc)
        self._cache[guild_id] = snaps
        return snaps

    async def _persist(self, guild_id: int, current: Dict[str, InviteSnap],
                       prev: Dict[str, InviteSnap]) -> None:
        gid = str(guild_id)
        now = utcnow()
        for code, snap in current.items():
            old = prev.get(code)
            if old and old.uses == snap.uses and old.max_uses == snap.max_uses and old.owner_id == snap.owner_id:
                continue
            await self.db.referral_invites.update_one(
                {"guild_id": gid, "code": code},
                {"$set": {"uses": snap.uses, "max_uses": snap.max_uses,
                          "inviter_id": str(snap.inviter_id) if snap.inviter_id else None,
                          "expires_at": snap.expires_at, "deleted": False, "updated_at": now},
                 "$setOnInsert": {"owner_id": str(snap.owner_id) if snap.owner_id else None,
                                  "is_personal": False, "created_at": now}},
                upsert=True,
            )
        for code in prev:
            if code not in current:
                await self.db.referral_invites.update_one(
                    {"guild_id": gid, "code": code}, {"$set": {"deleted": True, "updated_at": now}})

    # ── startup / events ──────────────────────────────────────────────────

    async def sync_guild(self, guild) -> Optional[int]:
        """(Re)build the snapshot for a guild. Returns invite count, or None if unavailable."""
        async with self.lock_for(guild.id):
            stored = await self._load_cache(guild.id)
            try:
                current = await self._fetch_current(guild, stored)
            except discord.Forbidden:
                if guild.id not in self._warned_forbidden:
                    self._warned_forbidden.add(guild.id)
                    logger.warning("Invite tracking unavailable in %s: bot lacks Manage Server", guild.name)
                return None
            except discord.HTTPException as e:
                logger.warning("Could not fetch invites for %s: %s", guild.name, e)
                return None

            missed = sum(max(0, s.uses - stored[c].uses) for c, s in current.items() if c in stored)
            if missed:
                logger.warning("%s: %d invite use(s) happened while the bot was offline "
                               "(cannot be attributed to a member).", guild.name, missed)
            await self._persist(guild.id, current, stored)
            self._cache[guild.id] = current
            return len(current)

    async def on_invite_created(self, invite) -> None:
        guild = getattr(invite, "guild", None)
        if guild is None:
            return
        async with self.lock_for(guild.id):
            cache = await self._load_cache(guild.id)
            doc = await self.db.referral_invites.find_one({"guild_id": str(guild.id), "code": invite.code})
            owner = int(doc["owner_id"]) if doc and doc.get("owner_id") else None
            snap = self._snap_from_invite(invite, owner)
            prev = {} if invite.code not in cache else {invite.code: cache[invite.code]}
            cache[invite.code] = snap
            await self._persist(guild.id, {invite.code: snap}, prev)

    async def on_invite_deleted(self, invite) -> None:
        guild = getattr(invite, "guild", None)
        if guild is None:
            return
        snap = (self._cache.get(guild.id) or {}).get(invite.code)
        if snap is not None:
            snap.deleted_at = time.time()       # keep it: it might be the invite just used up

    # ── attribution ───────────────────────────────────────────────────────

    async def resolve_join(self, guild, member) -> Attribution:
        async with self.lock_for(guild.id):
            prev = await self._load_cache(guild.id)
            try:
                current = await self._fetch_current(guild, prev)
            except discord.Forbidden:
                return Attribution("unavailable", reason="bot lacks Manage Server permission")
            except discord.HTTPException as e:
                return Attribution("unavailable", reason="Discord API error: {}".format(e))

            now = time.time()
            credits: Dict[str, int] = {}
            for code, snap in current.items():
                old = prev.get(code)
                delta = snap.uses - (old.uses if old else 0)
                if delta > 0:
                    credits[code] = delta

            carry = self._carry.setdefault(guild.id, {})
            for code in list(carry):
                n, exp = carry[code]
                if exp < now or code not in current:
                    del carry[code]
                else:
                    credits[code] = credits.get(code, 0) + n

            for code, old in prev.items():       # single-use invites removed by their last use
                if code in current or code == VANITY_KEY:
                    continue
                recent = old.deleted_at is None or (now - old.deleted_at) <= RECENT_DELETE_SECONDS
                if recent and old.max_uses > 0 and old.uses + 1 >= old.max_uses:
                    credits[code] = credits.get(code, 0) + 1

            lookup = dict(prev)
            lookup.update(current)
            result = self._decide(credits, lookup, carry, now)

            await self._persist(guild.id, current, prev)
            self._cache[guild.id] = current
            return result

    def _decide(self, credits, lookup, carry, now) -> Attribution:
        if len(credits) == 1:
            code, n = next(iter(credits.items()))
            carry.clear()
            if n > 1:
                carry[code] = (n - 1, now + CARRY_SECONDS)
            if code == VANITY_KEY:
                return Attribution("vanity", code="vanity", reason="joined through the vanity URL")
            snap = lookup.get(code)
            user = snap.credit_user if snap else None
            if user is None:
                return Attribution("none", code=code, reason="invite has no known creator")
            return Attribution("single", code=code, inviter_id=user)
        carry.clear()
        if len(credits) > 1:
            cands = []
            for code in credits:
                snap = lookup.get(code)
                cands.append({"code": code, "inviter_id": snap.credit_user if snap else None})
            return Attribution("ambiguous", candidates=cands,
                               reason="several invites changed at once")
        return Attribution("none", reason="no invite usage change detected")

    # ── /myinvite ─────────────────────────────────────────────────────────

    def _user_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        key = (guild_id, user_id)
        lock = self._user_locks.get(key)
        if lock is None:
            lock = self._user_locks[key] = asyncio.Lock()
        return lock

    async def get_or_create_personal_invite(self, guild, member, channel) -> str:
        """Reuse the member's personal invite, creating one only if none is alive."""
        async with self._user_lock(guild.id, member.id):
            gid, uid = str(guild.id), str(member.id)
            doc = await self.db.referral_invites.find_one(
                {"guild_id": gid, "owner_id": uid, "is_personal": True, "deleted": {"$ne": True}})
            if doc:
                alive = {inv.code: inv for inv in await guild.invites()}
                if doc["code"] in alive:
                    return alive[doc["code"]].url
                await self.db.referral_invites.update_one(
                    {"guild_id": gid, "code": doc["code"]}, {"$set": {"deleted": True}})

            invite = await channel.create_invite(
                max_age=0, max_uses=0, unique=True,
                reason="WWM referral invite for {} ({})".format(member, member.id))
            now = utcnow()
            await self.db.referral_invites.update_one(
                {"guild_id": gid, "code": invite.code},
                {"$set": {"owner_id": uid, "is_personal": True, "deleted": False,
                          "uses": int(invite.uses or 0), "max_uses": 0,
                          "inviter_id": str(invite.inviter.id) if getattr(invite, "inviter", None) else None,
                          "expires_at": None, "updated_at": now},
                 "$setOnInsert": {"created_at": now}},
                upsert=True)
            async with self.lock_for(guild.id):
                cache = await self._load_cache(guild.id)
                snap = self._snap_from_invite(invite, member.id)
                cache[invite.code] = snap
            return invite.url
