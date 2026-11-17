"""
WHERE WINDS MEET - Configuration
===================================
Central configuration file for all bot settings.
All values can be overridden via environment variables.
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()

_cfg_log = logging.getLogger("wwm.config")
CONFIG_WARNINGS: list = []


def _env_int(name: str, default: int, minimum: int = 0, maximum: int = 10**9) -> int:
    """Read an int env var; fall back to `default` (and remember a warning) if invalid."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        CONFIG_WARNINGS.append("{}={!r} is not a number - using {}".format(name, raw, default))
        return default
    if value < minimum or value > maximum:
        CONFIG_WARNINGS.append("{}={} out of range [{}, {}] - using {}".format(
            name, value, minimum, maximum, default))
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    CONFIG_WARNINGS.append("{}={!r} is not a boolean - using {}".format(name, raw, default))
    return default


def _env_id_list(name: str) -> list:
    """Comma separated Discord IDs; invalid entries are skipped with a warning."""
    out = []
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            out.append(int(part))
        else:
            CONFIG_WARNINGS.append("{} contains invalid ID {!r} - skipped".format(name, part))
    return out


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
    # REFERRAL / POINTS / LUCKY DRAW  (independent from XP)
    # Per-server values can be overridden with /referral config.
    # ─────────────────────────────────────────
    REFERRAL_ENABLED: bool = _env_bool("REFERRAL_ENABLED", True)
    REFERRAL_POINTS: int = _env_int("REFERRAL_POINTS", 3, 0, 1000)
    CHAT_REWARD_POINTS: int = _env_int("CHAT_REWARD_POINTS", 1, 0, 1000)
    MIN_ACCOUNT_AGE_DAYS: int = _env_int("MIN_ACCOUNT_AGE_DAYS", 7, 0, 3650)
    REFERRAL_CHANNEL_IDS: list = _env_id_list("REFERRAL_CHANNEL_IDS")
    REFERRAL_ANNOUNCEMENT_CHANNEL_ID: int = _env_int("REFERRAL_ANNOUNCEMENT_CHANNEL_ID", 0)
    REFERRAL_INVITE_CHANNEL_ID: int = _env_int("REFERRAL_INVITE_CHANNEL_ID", 0)
    REFERRAL_ADMIN_ROLE_IDS: list = _env_id_list("REFERRAL_ADMIN_ROLE_IDS")
    REFERRAL_DEFAULT_CAMPAIGN: str = os.getenv("REFERRAL_DEFAULT_CAMPAIGN", "").strip()
    MAX_DRAW_ENTRIES_PER_REQUEST: int = _env_int("MAX_DRAW_ENTRIES_PER_REQUEST", 10, 1, 100)

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
    def validate_referral(cls) -> list:
        """Startup validation for the reward system. Returns a list of problems."""
        problems = list(CONFIG_WARNINGS)
        if not cls.MONGO_URI:
            problems.append("MONGO_URI is empty")
        if not cls.DB_NAME:
            problems.append("DB_NAME is empty")
        return problems

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
