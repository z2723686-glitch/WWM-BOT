# -*- coding: utf-8 -*-
"""
WWM - Versioned database migrations for the referral / points / lucky draw system.

* Applied versions are recorded in the `schema_migrations` collection.
* Only pending versions run; every migration is idempotent (safe to re-run).
* Migrations only CREATE collections and indexes. They never update, rename or
  delete existing collections/documents (users, XP, bound_accounts, giveaways...).
* A failed migration is logged and recorded as "failed"; it is retried on the next
  start. Completed ones are never repeated.
"""

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List

from pymongo import ASCENDING, DESCENDING, IndexModel

from utils.reward_common import utcnow

logger = logging.getLogger("wwm.migrations")


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    run: Callable[..., Awaitable[None]]


async def _ensure_collection(db, name: str) -> None:
    existing = await db.list_collection_names()
    if name not in existing:
        try:
            await db.create_collection(name)
        except Exception as e:  # raced with another process / already exists
            if "already exists" not in str(e).lower():
                raise


# Each entry: collection -> list of IndexModel (all named; names are unique per collection)
_V1_INDEXES = {
    "referral_users": [
        IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING)], unique=True, name="ru_guild_user_unique"),
        IndexModel([("guild_id", ASCENDING), ("total_points_earned", DESCENDING)], name="ru_guild_earned"),
    ],
    "referral_invites": [
        IndexModel([("guild_id", ASCENDING), ("code", ASCENDING)], unique=True, name="ri_guild_code_unique"),
        IndexModel([("guild_id", ASCENDING), ("owner_id", ASCENDING)], name="ri_guild_owner"),
    ],
    "referrals": [
        IndexModel([("guild_id", ASCENDING), ("invited_user_id", ASCENDING)], unique=True, name="rf_guild_invited_unique"),
        IndexModel([("guild_id", ASCENDING), ("inviter_id", ASCENDING), ("reward_status", ASCENDING)], name="rf_guild_inviter_reward"),
        IndexModel([("guild_id", ASCENDING), ("validation_status", ASCENDING)], name="rf_guild_validation"),
    ],
    "reward_transactions": [
        IndexModel([("idempotency_key", ASCENDING)], unique=True, name="rt_idempotency_unique"),
        IndexModel([("transaction_id", ASCENDING)], unique=True, name="rt_transaction_unique"),
        IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING), ("created_at", DESCENDING)], name="rt_guild_user_time"),
        IndexModel([("status", ASCENDING), ("created_at", ASCENDING)], name="rt_status_time"),
    ],
    "chat_reward_claims": [
        IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING)], unique=True, name="cc_guild_user_unique"),
    ],
    "points_draws": [
        IndexModel([("draw_id", ASCENDING)], unique=True, name="pd_draw_unique"),
        IndexModel([("guild_id", ASCENDING), ("status", ASCENDING)], name="pd_guild_status"),
    ],
    "points_draw_entries": [
        IndexModel([("request_key", ASCENDING)], unique=True, name="pe_request_unique"),
        IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING), ("created_at", DESCENDING)], name="pe_guild_user_time"),
        IndexModel([("draw_id", ASCENDING), ("user_id", ASCENDING)], name="pe_draw_user"),
        IndexModel([("status", ASCENDING), ("created_at", ASCENDING)], name="pe_status_time"),
    ],
    "points_prizes": [
        IndexModel([("prize_id", ASCENDING)], unique=True, name="pp_prize_unique"),
        IndexModel([("guild_id", ASCENDING), ("campaign_id", ASCENDING), ("active", ASCENDING)], name="pp_guild_campaign_active"),
    ],
    "points_prize_winners": [
        IndexModel([("winner_id", ASCENDING)], unique=True, name="pw_winner_unique"),
        IndexModel([("request_key", ASCENDING), ("spin_index", ASCENDING)], unique=True, name="pw_request_spin_unique"),
        IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING), ("created_at", DESCENDING)], name="pw_guild_user_time"),
        IndexModel([("guild_id", ASCENDING), ("status", ASCENDING)], name="pw_guild_status"),
    ],
    "referral_audit_logs": [
        IndexModel([("guild_id", ASCENDING), ("created_at", DESCENDING)], name="ra_guild_time"),
    ],
    "schema_migrations": [
        IndexModel([("version", ASCENDING)], unique=True, name="sm_version_unique"),
    ],
}

_V2_INDEXES = {
    "referral_config": [
        IndexModel([("guild_id", ASCENDING)], unique=True, name="rc_guild_unique"),
    ],
}


async def _create_from_spec(db, spec: dict) -> None:
    for name, indexes in spec.items():
        await _ensure_collection(db, name)
        await db[name].create_indexes(indexes)


async def _v1_reward_collections(db) -> None:
    await _create_from_spec(db, _V1_INDEXES)


async def _v2_reward_config(db) -> None:
    await _create_from_spec(db, _V2_INDEXES)


MIGRATIONS: List[Migration] = [
    Migration("2026_11_17_001_reward_collections",
              "Create referral/points/lucky-draw collections and indexes", _v1_reward_collections),
    Migration("2026_11_17_002_reward_config",
              "Create per-server reward settings collection", _v2_reward_config),
]


async def run_pending_migrations(db) -> dict:
    """
    Run all pending migrations in order. `db` is the Motor database object.
    Returns {"applied": [...], "skipped": [...], "failed": [...]}.
    Never raises for a single failed migration - callers inspect "failed".
    """
    result = {"applied": [], "skipped": [], "failed": []}

    # schema_migrations must exist with its unique index before anything else
    await _ensure_collection(db, "schema_migrations")
    await db["schema_migrations"].create_indexes(_V1_INDEXES["schema_migrations"])

    done = set()
    async for doc in db["schema_migrations"].find({"status": "applied"}):
        done.add(doc["version"])

    for mig in MIGRATIONS:
        if mig.version in done:
            result["skipped"].append(mig.version)
            continue
        started = time.monotonic()
        try:
            await mig.run(db)
            await db["schema_migrations"].update_one(
                {"version": mig.version},
                {"$set": {
                    "version": mig.version,
                    "description": mig.description,
                    "status": "applied",
                    "applied_at": utcnow(),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "error": None,
                }},
                upsert=True,
            )
            result["applied"].append(mig.version)
            logger.info("Migration applied: %s", mig.version)
        except Exception as e:
            logger.error("Migration FAILED: %s: %s", mig.version, e)
            result["failed"].append(mig.version)
            try:
                await db["schema_migrations"].update_one(
                    {"version": mig.version},
                    {"$set": {"version": mig.version, "description": mig.description,
                              "status": "failed", "failed_at": utcnow(), "error": str(e)[:500]}},
                    upsert=True,
                )
            except Exception:
                pass
            break  # later migrations may depend on this one
    return result
