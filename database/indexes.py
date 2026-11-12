"""
WHERE WINDS MEET - Database Indexes
======================================
Standalone script to initialize MongoDB indexes.
Run this once during setup: python database/indexes.py

Also called automatically on bot startup via database.py.
"""

import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, IndexModel
from dotenv import load_dotenv

load_dotenv()


async def create_all_indexes():
    """Create all required MongoDB indexes for optimal performance."""
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.getenv("DB_NAME", "wwm_bot")

    print(f"🔌 Connecting to MongoDB...")
    client = AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=10000)

    try:
        await client.admin.command("ping")
        print(f"✅ Connected to MongoDB")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return

    db = client[db_name]
    print(f"📦 Using database: {db_name}")

    # ── USERS ────────────────────────────────
    print("\n📝 Creating indexes for 'users'...")
    await db.users.create_indexes([
        IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING)], unique=True, name="guild_user_unique"),
        IndexModel([("guild_id", ASCENDING), ("xp", DESCENDING)], name="guild_xp_desc"),
        IndexModel([("guild_id", ASCENDING), ("level", DESCENDING)], name="guild_level_desc"),
        IndexModel([("user_id", ASCENDING)], name="user_id_asc"),
    ])
    print("   ✅ users indexes created")

    # ── GUILD SETTINGS ────────────────────────
    print("📝 Creating indexes for 'guild_settings'...")
    await db.guild_settings.create_indexes([
        IndexModel([("guild_id", ASCENDING)], unique=True, name="guild_id_unique"),
    ])
    print("   ✅ guild_settings indexes created")

    # ── ROLE REWARDS ─────────────────────────
    print("📝 Creating indexes for 'role_rewards'...")
    await db.role_rewards.create_indexes([
        IndexModel([("guild_id", ASCENDING), ("level", ASCENDING)], unique=True, name="guild_level_unique"),
    ])
    print("   ✅ role_rewards indexes created")

    # ── WARNINGS ─────────────────────────────
    print("📝 Creating indexes for 'warnings'...")
    await db.warnings.create_indexes([
        IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING)], name="guild_user_warn"),
        IndexModel([("guild_id", ASCENDING), ("timestamp", DESCENDING)], name="guild_timestamp"),
    ])
    print("   ✅ warnings indexes created")

    # ── REACTION LOGS ─────────────────────────
    print("📝 Creating indexes for 'reaction_logs'...")
    await db.reaction_logs.create_indexes([
        IndexModel(
            [("guild_id", ASCENDING), ("giver_id", ASCENDING),
             ("receiver_id", ASCENDING), ("message_id", ASCENDING)],
            name="reaction_dedup"
        ),
        IndexModel([("timestamp", DESCENDING)], name="reaction_timestamp"),
        # TTL: auto-delete entries older than 24 hours
        IndexModel([("timestamp", ASCENDING)], expireAfterSeconds=86400, name="reaction_ttl"),
    ])
    print("   ✅ reaction_logs indexes created (with 24h TTL)")

    # ── VOICE ACTIVITY ────────────────────────
    print("📝 Creating indexes for 'voice_activity'...")
    await db.voice_activity.create_indexes([
        IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING)], unique=True, name="voice_guild_user"),
        IndexModel([("last_join", ASCENDING)], name="voice_last_join"),
    ])
    print("   ✅ voice_activity indexes created")

    # ── WELCOME SETTINGS ──────────────────────
    print("📝 Creating indexes for 'welcome_settings'...")
    await db.welcome_settings.create_indexes([
        IndexModel([("guild_id", ASCENDING)], unique=True, name="welcome_guild_unique"),
    ])
    print("   ✅ welcome_settings indexes created")

    # ── OAUTH USERS ───────────────────────────
    print("📝 Creating indexes for 'oauth_users'...")
    await db.oauth_users.create_indexes([
        IndexModel([("user_id", ASCENDING)], unique=True, name="oauth_user_unique"),
        IndexModel([("access_token", ASCENDING)], name="oauth_token"),
        # TTL: sessions expire after 7 days
        IndexModel([("created_at", ASCENDING)], expireAfterSeconds=604800, name="oauth_ttl"),
    ])
    print("   ✅ oauth_users indexes created (with 7-day TTL)")

    # ── SERVER PERMISSIONS ────────────────────
    print("📝 Creating indexes for 'server_permissions'...")
    await db.server_permissions.create_indexes([
        IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING)], unique=True, name="perm_guild_user"),
    ])
    print("   ✅ server_permissions indexes created")

    # ── ADMIN LOGS ────────────────────────────
    print("📝 Creating indexes for 'admin_logs'...")
    await db.admin_logs.create_indexes([
        IndexModel([("guild_id", ASCENDING), ("timestamp", DESCENDING)], name="admin_guild_time"),
        IndexModel([("action_by", ASCENDING)], name="admin_action_by"),
        # TTL: logs expire after 90 days
        IndexModel([("timestamp", ASCENDING)], expireAfterSeconds=7776000, name="admin_ttl"),
    ])
    print("   ✅ admin_logs indexes created (with 90-day TTL)")

    client.close()
    print("\n🎉 All indexes created successfully!")
    print(f"   Database: {db_name}")
    print(f"   Collections initialized: 10")


if __name__ == "__main__":
    asyncio.run(create_all_indexes())
