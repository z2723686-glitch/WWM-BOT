"""
WHERE WINDS MEET - Database Manager
======================================
Async MongoDB manager using Motor driver.
Handles all database connections, collections, and indexes.
Optimized for high-performance async queries.
"""

import logging
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, IndexModel

from config import Config

logger = logging.getLogger("wwm.database")


class Database:
    """
    Central database manager for the WWM Bot.
    Uses Motor for async MongoDB operations.
    """

    def __init__(self):
        self.client: AsyncIOMotorClient = None
        self.db = None

        
        self.users = None
        self.guild_settings = None
        self.role_rewards = None
        self.warnings = None
        self.reaction_logs = None
        self.voice_activity = None
        self.welcome_settings = None
        self.oauth_users = None
        self.server_permissions = None
        self.admin_logs = None
        self.guild_applications = None
        self.bound_accounts    = None
        self.giveaways         = None
        self.automod_settings  = None

    async def connect(self):
        
        try:
            self.client = AsyncIOMotorClient(
                Config.MONGO_URI,
                serverSelectionTimeoutMS=10000,
                connectTimeoutMS=10000,
                socketTimeoutMS=10000,
                maxPoolSize=50,     
                minPoolSize=5,
                retryWrites=True,
            )

           
            await self.client.admin.command("ping")

            self.db = self.client[Config.DB_NAME]

           
            self.users = self.db["users"]
            self.guild_settings = self.db["guild_settings"]
            self.role_rewards = self.db["role_rewards"]
            self.warnings = self.db["warnings"]
            self.reaction_logs = self.db["reaction_logs"]
            self.voice_activity = self.db["voice_activity"]
            self.welcome_settings = self.db["welcome_settings"]
            self.oauth_users = self.db["oauth_users"]
            self.server_permissions = self.db["server_permissions"]
            self.admin_logs = self.db["admin_logs"]
            self.guild_applications = self.db["guild_applications"]
            self.bound_accounts     = self.db["bound_accounts"]
            self.giveaways          = self.db["giveaways"]
            self.automod_settings   = self.db["automod_settings"]

            # Create optimized indexes
            await self._create_indexes()

            logger.info(f"✅ Connected to MongoDB: {Config.DB_NAME}")

        except Exception as e:
            logger.critical(f"❌ MongoDB connection failed: {e}")
            raise

    async def _create_indexes(self):
        """Create optimized indexes for all collections."""
        try:
            # Users collection - most queried
            await self.users.create_indexes([
                IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING)], unique=True),
                IndexModel([("guild_id", ASCENDING), ("xp", DESCENDING)]),      # Leaderboard
                IndexModel([("guild_id", ASCENDING), ("level", DESCENDING)]),   # Level queries
                IndexModel([("user_id", ASCENDING)]),                           # User lookup
            ])

            # Guild settings
            await self.guild_settings.create_indexes([
                IndexModel([("guild_id", ASCENDING)], unique=True),
            ])

            # Role rewards
            await self.role_rewards.create_indexes([
                IndexModel([("guild_id", ASCENDING), ("level", ASCENDING)], unique=True),
            ])

            # Warnings
            await self.warnings.create_indexes([
                IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING)]),
                IndexModel([("guild_id", ASCENDING), ("timestamp", DESCENDING)]),
            ])

            # Reaction logs - prevent duplicate XP
            await self.reaction_logs.create_indexes([
                IndexModel([
                    ("guild_id", ASCENDING),
                    ("giver_id", ASCENDING),
                    ("receiver_id", ASCENDING),
                    ("message_id", ASCENDING)
                ]),
                IndexModel([("timestamp", DESCENDING)]),
                # TTL index - auto-delete reaction logs after 24 hours
                IndexModel([("timestamp", ASCENDING)], expireAfterSeconds=86400),
            ])

            # Voice activity
            await self.voice_activity.create_indexes([
                IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING)], unique=True),
                IndexModel([("last_join", ASCENDING)]),
            ])

            # Welcome settings
            await self.welcome_settings.create_indexes([
                IndexModel([("guild_id", ASCENDING)], unique=True),
            ])

            # OAuth users
            await self.oauth_users.create_indexes([
                IndexModel([("user_id", ASCENDING)], unique=True),
                IndexModel([("access_token", ASCENDING)]),
                # TTL - expire sessions after 7 days
                IndexModel([("created_at", ASCENDING)], expireAfterSeconds=604800),
            ])

            # Server permissions
            await self.server_permissions.create_indexes([
                IndexModel([("guild_id", ASCENDING), ("user_id", ASCENDING)], unique=True),
            ])

            # Admin logs
            await self.admin_logs.create_indexes([
                IndexModel([("guild_id", ASCENDING), ("timestamp", DESCENDING)]),
                IndexModel([("action_by", ASCENDING)]),
            ])
            
            # AutoMod settings (one document per guild)

            await self.automod_settings.create_indexes([
                IndexModel([("guild_id", ASCENDING)], unique=True),
            ])

            logger.info("✅ Database indexes created successfully")

        except Exception as e:
            logger.error(f"❌ Error creating indexes: {e}")

    async def close(self):
        """Close the database connection gracefully."""
        if self.client:
            self.client.close()
            logger.info("🔴 Database connection closed")

    # ─────────────────────────────────────────
    # USER OPERATIONS
    # ─────────────────────────────────────────

    async def get_user(self, guild_id: str, user_id: str) -> dict:
        """Get or create a user document."""
        user = await self.users.find_one(
            {"guild_id": guild_id, "user_id": user_id}
        )
        if not user:
            user = await self.create_user(guild_id, user_id)
        return user

    async def create_user(self, guild_id: str, user_id: str) -> dict:
        """Create a new user document with defaults."""
        doc = {
            "guild_id": guild_id,
            "user_id": user_id,
            "xp": 0,
            "level": 0,
            "messages": 0,
            "voice_minutes": 0,
            "reactions_given": 0,
            "reactions_received": 0,
        }
        await self.users.update_one(
            {"guild_id": guild_id, "user_id": user_id},
            {"$setOnInsert": doc},
            upsert=True
        )
        return doc

    async def update_user_xp(self, guild_id: str, user_id: str, xp_add: int) -> dict:
        """Add XP to a user and return updated document."""
        return await self.users.find_one_and_update(
            {"guild_id": guild_id, "user_id": user_id},
            {
                "$inc": {"xp": xp_add},
                "$setOnInsert": {
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "level": 0,
                    "messages": 0,
                    "voice_minutes": 0
                }
            },
            upsert=True,
            return_document=True
        )

    async def get_leaderboard(self, guild_id: str, page: int = 0, limit: int = 10) -> list:
        """Get paginated leaderboard for a guild."""
        cursor = self.users.find(
            {"guild_id": guild_id},
            {"user_id": 1, "xp": 1, "level": 1, "messages": 1}
        ).sort("xp", DESCENDING).skip(page * limit).limit(limit)
        return await cursor.to_list(length=limit)

    async def get_user_rank(self, guild_id: str, user_id: str) -> int:
        """Get a user's rank position in the guild."""
        user = await self.get_user(guild_id, user_id)
        rank = await self.users.count_documents(
            {"guild_id": guild_id, "xp": {"$gt": user.get("xp", 0)}}
        )
        return rank + 1

    # ─────────────────────────────────────────
    # GUILD SETTINGS OPERATIONS
    # ─────────────────────────────────────────

    async def get_guild_settings(self, guild_id: str) -> dict:
        """Get guild settings, create defaults if not exist."""
        settings = await self.guild_settings.find_one({"guild_id": guild_id})
        if not settings:
            settings = {
                "guild_id": guild_id,
                "xp_per_message": 15,
                "xp_cooldown": 60,
                "level_up_channel": None,
                "log_channel": None,
                "welcome_channel": None,
                "voice_xp_enabled": True,
                "voice_xp_amount": 10,
                "reaction_xp_enabled": True,
            }
            await self.guild_settings.insert_one(settings)
        return settings

    async def update_guild_settings(self, guild_id: str, update: dict) -> None:
        """Update guild settings."""
        await self.guild_settings.update_one(
            {"guild_id": guild_id},
            {"$set": update},
            upsert=True
        )

    # ─────────────────────────────────────────
    # ROLE REWARDS OPERATIONS
    # ─────────────────────────────────────────

    async def get_role_rewards(self, guild_id: str) -> list:
        """Get all role rewards for a guild sorted by level."""
        cursor = self.role_rewards.find(
            {"guild_id": guild_id}
        ).sort("level", ASCENDING)
        return await cursor.to_list(length=100)

    async def set_role_reward(self, guild_id: str, level: int, role_id: str) -> None:
        """Set a role reward for a specific level."""
        await self.role_rewards.update_one(
            {"guild_id": guild_id, "level": level},
            {"$set": {"guild_id": guild_id, "level": level, "role_id": role_id}},
            upsert=True
        )

    async def remove_role_reward(self, guild_id: str, level: int) -> None:
        """Remove a role reward."""
        await self.role_rewards.delete_one({"guild_id": guild_id, "level": level})

    # ─────────────────────────────────────────
    # WARNINGS OPERATIONS
    # ─────────────────────────────────────────

    async def add_warning(self, guild_id: str, user_id: str, mod_id: str, reason: str) -> int:
        """Add a warning and return total warning count."""
        from datetime import datetime
        await self.warnings.insert_one({
            "guild_id": guild_id,
            "user_id": user_id,
            "mod_id": mod_id,
            "reason": reason,
            "timestamp": datetime.utcnow()
        })
        return await self.warnings.count_documents(
            {"guild_id": guild_id, "user_id": user_id}
        )

    async def get_warnings(self, guild_id: str, user_id: str) -> list:
        """Get all warnings for a user."""
        cursor = self.warnings.find(
            {"guild_id": guild_id, "user_id": user_id}
        ).sort("timestamp", DESCENDING)
        return await cursor.to_list(length=50)

    # ─────────────────────────────────────────
    # ADMIN LOG OPERATIONS
    # ─────────────────────────────────────────

    async def log_admin_action(self, guild_id: str, action_by: str, action: str, target: str, reason: str) -> None:
        """Log an admin/moderation action."""
        from datetime import datetime
        await self.admin_logs.insert_one({
            "guild_id": guild_id,
            "action_by": action_by,
            "action": action,
            "target": target,
            "reason": reason,
            "timestamp": datetime.utcnow()
        })
        
    # ─────────────────────────────────────────
    # AUTOMOD (slang & link moderation) OPERATIONS
    # ─────────────────────────────────────────

    async def get_automod_settings(self, guild_id: str) -> dict:
        """Get a guild's AutoMod settings (empty defaults if none saved yet)."""
        doc = await self.automod_settings.find_one({"guild_id": guild_id})
        return doc or {
            "guild_id": guild_id,
            "channels": {},
            "whitelist": [],
            "custom_slang": [],
            "removed_slang": [],
            "warn_on_violation": False,
        }

    async def update_automod_settings(self, guild_id: str, ops: dict) -> None:
        """Apply raw MongoDB update operators ($set, $unset, $addToSet, $pull)."""
        await self.automod_settings.update_one(
            {"guild_id": guild_id}, ops, upsert=True
        )
