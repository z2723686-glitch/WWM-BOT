"""
WHERE WINDS MEET - Configuration
===================================
Central configuration file for all bot settings.
All values can be overridden via environment variables.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Central configuration class for the WWM Bot."""

    # ─────────────────────────────────────────
    # BOT CORE SETTINGS
    # ─────────────────────────────────────────
    BOT_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    PREFIX: str = os.getenv("BOT_PREFIX", "wwm!")
    BOT_NAME: str = "Where Winds Meet"
    BOT_VERSION: str = "2.0.0"

    # ─────────────────────────────────────────
    # DATABASE SETTINGS
    # ─────────────────────────────────────────
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    DB_NAME: str = os.getenv("DB_NAME", "wwm_bot")

    # ─────────────────────────────────────────
    # XP & LEVELING SETTINGS
    # ─────────────────────────────────────────
    DEFAULT_XP_PER_MESSAGE: int = int(os.getenv("XP_PER_MESSAGE", "15"))
    DEFAULT_XP_COOLDOWN: int = int(os.getenv("XP_COOLDOWN", "60"))       # seconds
    XP_MIN: int = 10           # Minimum XP per message
    XP_MAX: int = 25           # Maximum XP per message (randomized)
    VOICE_XP_INTERVAL: int = 5  # Minutes between voice XP grants
    VOICE_XP_AMOUNT: int = 10   # XP per voice interval
    REACTION_XP_GIVE: int = 2   # XP for giving a reaction
    REACTION_XP_RECEIVE: int = 5  # XP for receiving a reaction
    REACTION_XP_COOLDOWN: int = 30  # Seconds between reaction XP

    # ─────────────────────────────────────────
    # LEVELING FORMULA
    # Level N requires: BASE * N^EXPONENT XP
    # ─────────────────────────────────────────
    XP_BASE: int = 100
    XP_EXPONENT: float = 1.5

    # ─────────────────────────────────────────
    # SUPPORTED REACTION EMOJIS FOR XP
    # ─────────────────────────────────────────
    XP_REACTIONS: list = ["❤️", "👍", "🔥", "😂", "😍"]

    # ─────────────────────────────────────────
    # DEFAULT ROLE REWARDS
    # ─────────────────────────────────────────
    DEFAULT_ROLE_REWARDS: dict = {
        5: "Hunter",
        10: "Elite",
        20: "Veteran",
        30: "Legend"
    }

    # ─────────────────────────────────────────
    # MODERATION SETTINGS
    # ─────────────────────────────────────────
    MAX_WARNINGS_BEFORE_BAN: int = 3
    TIMEOUT_DURATION_DEFAULT: int = 10  # minutes

    # ─────────────────────────────────────────
    # GUILD JOIN SYSTEM
    # ─────────────────────────────────────────
    GUILD_JOIN_PANEL_CHANNEL: int = int(os.getenv("GUILD_JOIN_PANEL_CHANNEL", "0"))
    GUILD_REQUEST_CHANNEL: int = int(os.getenv("GUILD_REQUEST_CHANNEL", "0"))
    GUILD_MEMBER_ROLE: int = int(os.getenv("GUILD_MEMBER_ROLE", "0"))
    GUILD_MOD_ROLES: list = [
        int(r.strip())
        for r in os.getenv("GUILD_MOD_ROLES", "").split(",")
        if r.strip().isdigit()
    ]
    GAME_GUILD_ID: str = os.getenv("GAME_GUILD_ID", "YOUR_GUILD_ID")

    # ── Account Binding & Giveaway ─────────────────────────────
    BIND_CHANNEL: int     = int(os.getenv("BIND_CHANNEL",     "0"))
    BIND_LOG_CHANNEL: int = int(os.getenv("BIND_LOG_CHANNEL", "0"))
    GIVEAWAY_CHANNEL: int = int(os.getenv("GIVEAWAY_CHANNEL", "0"))

    # ─────────────────────────────────────────
    # WEB DASHBOARD SETTINGS
    # ─────────────────────────────────────────
    DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    DASHBOARD_PORT: int = int(os.getenv("PORT", "8080"))
    SECRET_KEY: str = os.getenv("SECRET_KEY", "change-this-in-production-railway")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_HOURS: int = 24

    # ─────────────────────────────────────────
    # DISCORD OAUTH2 SETTINGS
    # ─────────────────────────────────────────
    CLIENT_ID: str = os.getenv("DISCORD_CLIENT_ID", "")
    CLIENT_SECRET: str = os.getenv("DISCORD_CLIENT_SECRET", "")
    REDIRECT_URI: str = os.getenv("REDIRECT_URI", "http://localhost:8080/auth/callback")
    OAUTH2_SCOPE: str = "identify guilds"
    DISCORD_API_BASE: str = "https://discord.com/api/v10"

    # ─────────────────────────────────────────
    # BOT INVITE PERMISSIONS
    # ─────────────────────────────────────────
    BOT_PERMISSIONS: int = 8  # Administrator (use specific perms in production)
    BOT_INVITE_PERMS: int = (
        0x8 |      # Administrator (for full functionality)
        0x400 |    # Manage Roles
        0x2 |      # Kick Members
        0x4 |      # Ban Members
        0x8000 |   # Manage Channels
        0x800 |    # Add Reactions
        0x10000 |  # Read Message History
        0x2000     # Manage Messages
    )

    # ─────────────────────────────────────────
    # EMBED COLORS (Premium Dark Theme)
    # ─────────────────────────────────────────
    COLOR_PRIMARY: int = 0xFFD700    # Gold
    COLOR_SUCCESS: int = 0x2ECC71    # Green
    COLOR_ERROR: int = 0xE74C3C      # Red
    COLOR_WARNING: int = 0xF39C12    # Orange
    COLOR_INFO: int = 0x3498DB       # Blue
    COLOR_DARK: int = 0x2C2F33      # Discord dark
    COLOR_PURPLE: int = 0x9B59B6    # Purple accent

    # ─────────────────────────────────────────
    # RANK CARD DESIGN
    # ─────────────────────────────────────────
    RANK_CARD_WIDTH: int = 900
    RANK_CARD_HEIGHT: int = 280
    RANK_CARD_BG_COLOR: tuple = (18, 18, 30)  # Very dark navy
    RANK_CARD_ACCENT: tuple = (255, 215, 0)   # Gold

    # ─────────────────────────────────────────
    # RATE LIMITING
    # ─────────────────────────────────────────
    API_RATE_LIMIT: int = 60      # Requests per minute
    API_RATE_WINDOW: int = 60     # Window in seconds

    # ─────────────────────────────────────────
    # LEADERBOARD
    # ─────────────────────────────────────────
    LEADERBOARD_PAGE_SIZE: int = 10

    # ─────────────────────────────────────────
    # LOGGING
    # ─────────────────────────────────────────
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = "wwm_bot.log"

    @classmethod
    def get_invite_url(cls) -> str:
        """Generate bot invite URL with proper permissions."""
        return (
            f"https://discord.com/api/oauth2/authorize"
            f"?client_id={cls.CLIENT_ID}"
            f"&permissions={cls.BOT_INVITE_PERMS}"
            f"&scope=bot%20applications.commands"
        )

    @classmethod
    def get_oauth_url(cls) -> str:
        """Generate Discord OAuth2 URL for dashboard login."""
        return (
            f"https://discord.com/api/oauth2/authorize"
            f"?client_id={cls.CLIENT_ID}"
            f"&redirect_uri={cls.REDIRECT_URI}"
            f"&response_type=code"
            f"&scope=identify%20guilds"
  )
