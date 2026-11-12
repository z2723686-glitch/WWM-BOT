# -*- coding: utf-8 -*-
"""
WWM - Scheduled Events System
Fixes vs previous version:
  1. Check window is the entire current minute (not exact :30s match).
     Any check within HH:MM will fire the event — so a 30s loop can never
     miss a slot due to timing jitter or a slow restart.
  2. Admin log channel 403 is caught silently — no error spam in logs.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from utils.permissions import is_admin

logger = logging.getLogger("wwm.events")

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "events_config.json"
)

DAY_NAME_TO_INDEX = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
INDEX_TO_DAY_NAME = {v: k for k, v in DAY_NAME_TO_INDEX.items()}

CHECK_INTERVAL_SECONDS = 30


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("events_config.json not found at %s", CONFIG_PATH)
        return {"timezone": "Asia/Dhaka", "channel_id": "", "events": {}}
    except json.JSONDecodeError as e:
        logger.error("events_config.json is invalid JSON: %s", e)
        return {"timezone": "Asia/Dhaka", "channel_id": "", "events": {}}


def _save_config(data: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error("Failed to save events_config.json: %s", e)
        return False


class ScheduledEvents(commands.Cog):
    """Weekly recurring announcement scheduler."""

    def __init__(self, bot):
        self.bot = bot
        self.config = _load_config()
        self.tz = ZoneInfo(self.config.get("timezone", "Asia/Dhaka"))
        self._sent_log_path = os.path.join(
            os.path.dirname(CONFIG_PATH), "sent_log.json"
        )
        self._sent_keys = self._load_sent_log()
        self.scheduler_loop.start()

    def cog_unload(self):
        self.scheduler_loop.cancel()

    # ── Dedup persistence ─────────────────────────────────────

    def _load_sent_log(self) -> set:
        try:
            with open(self._sent_log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("sent", []))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save_sent_log(self):
        try:
            trimmed = list(self._sent_keys)[-200:]
            with open(self._sent_log_path, "w", encoding="utf-8") as f:
                json.dump({"sent": trimmed}, f)
        except Exception as e:
            logger.error("Failed to save sent_log.json: %s", e)

    # ── Core scheduler loop ───────────────────────────────────

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def scheduler_loop(self):
        try:
            now          = datetime.now(self.tz)
            current_day  = INDEX_TO_DAY_NAME[now.weekday()]
            current_hm   = now.strftime("%H:%M")   # "HH:MM" of current minute
            week_num     = now.isocalendar()[1]
            year         = now.isocalendar()[0]

            channel_id = self.config.get("channel_id", "")
            if not channel_id:
                return

            for event_key, event in self.config.get("events", {}).items():
                if not event.get("enabled", True):
                    continue

                for slot in event.get("schedule", []):
                    if slot["day"] != current_day:
                        continue

                    # ── FIX 1: match the whole current minute ──────────────
                    # The loop fires every 30s. As long as we're inside the
                    # same HH:MM minute, we consider it a match.
                    # The dedup key (which includes year+week+day+time) ensures
                    # we only send once per week per slot even if this block
                    # runs twice within the same minute.
                    if slot["time"] != current_hm:
                        continue

                    dedup_key = "{}|{}-W{}|{}|{}".format(
                        event_key, year, week_num, slot["day"], slot["time"]
                    )
                    if dedup_key in self._sent_keys:
                        continue   # already sent this slot this week

                    await self._send_announcement(event_key, event, channel_id)
                    self._sent_keys.add(dedup_key)
                    self._save_sent_log()

        except Exception as e:
            logger.error("Scheduler loop error: %s", e)

    @scheduler_loop.before_loop
    async def before_scheduler(self):
        await self.bot.wait_until_ready()
        logger.info(
            "Event scheduler started - timezone=%s, interval=%ds",
            self.config.get("timezone"), CHECK_INTERVAL_SECONDS
        )

    # ── Send logic ────────────────────────────────────────────

    async def _send_announcement(self, event_key: str, event: dict, channel_id: str):
        try:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                channel = await self.bot.fetch_channel(int(channel_id))

            await channel.send(
                event["message"],
                allowed_mentions=discord.AllowedMentions(
                    everyone=True, roles=True, users=True
                ),
            )
            logger.info(
                "Sent scheduled announcement: %s -> #%s",
                event.get("name", event_key), channel_id
            )

        except discord.Forbidden:
            logger.error(
                "Bot missing permission to send in channel %s for event '%s'. "
                "Grant the bot Send Messages permission in that channel.",
                channel_id, event_key
            )
        except Exception as e:
            logger.error("Failed to send announcement '%s': %s", event_key, e)

    # ── Next occurrence ────────────────────────────────────────

    def _next_occurrence(self, schedule: list) -> str:
        now  = datetime.now(self.tz)
        best = None
        for slot in schedule:
            target_day = DAY_NAME_TO_INDEX[slot["day"]]
            hh, mm = map(int, slot["time"].split(":"))
            days_ahead = (target_day - now.weekday()) % 7
            candidate  = (now + timedelta(days=days_ahead)).replace(
                hour=hh, minute=mm, second=0, microsecond=0
            )
            if candidate <= now:
                candidate += timedelta(days=7)
            if best is None or candidate < best:
                best = candidate
        if best is None:
            return "Not scheduled"
        ts = int(best.timestamp())
        return "<t:{}:F> (<t:{}:R>)".format(ts, ts)

    # ── Admin log ─────────────────────────────────────────────

    async def _log_admin_action(
        self, interaction: discord.Interaction, action: str, event_key: str
    ):
        logger.info(
            "Admin action: %s by %s (%s) on event '%s'",
            action, interaction.user, interaction.user.id, event_key
        )
        # ── FIX 2: silently skip if bot lacks access to log channel ────────
        try:
            settings       = await self.bot.db.get_guild_settings(str(interaction.guild.id))
            log_channel_id = settings.get("log_channel")
            if not log_channel_id:
                return

            channel = interaction.guild.get_channel(int(log_channel_id))
            if not channel:
                return

            embed = discord.Embed(
                title="Scheduled Event - Admin Action",
                description="**Action:** {}\n**Event:** `{}`\n**By:** {}".format(
                    action, event_key, interaction.user.mention
                ),
                color=Config.COLOR_INFO,
                timestamp=datetime.now(self.tz),
            )
            await channel.send(embed=embed)

        except discord.Forbidden:
            # Bot can't access the log channel — log a warning once, don't spam errors
            logger.warning(
                "Cannot post to log channel %s — bot missing access. "
                "Grant Send Messages permission or clear log channel with /setlogchannel.",
                settings.get("log_channel") if "settings" in dir() else "unknown"
            )
        except Exception as e:
            logger.warning("Admin log skipped: %s", e)

    # ═══════════════════════════════════════════════════════════
    # SLASH COMMAND GROUP
    # ═══════════════════════════════════════════════════════════

    event_group = app_commands.Group(
        name="event",
        description="Manage scheduled guild announcements"
    )

    @event_group.command(name="list", description="Show all scheduled events")
    @is_admin()
    async def event_list(self, interaction: discord.Interaction):
        events = self.config.get("events", {})
        if not events:
            await interaction.response.send_message(
                "No events configured.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="\U0001f4c5 Scheduled Events",
            description="Timezone: `{}`".format(self.config.get("timezone")),
            color=Config.COLOR_INFO,
        )
        for key, ev in events.items():
            status   = "\U0001f7e2 Enabled" if ev.get("enabled", True) else "\U0001f534 Disabled"
            days_str = ", ".join(
                "{} {}".format(s["day"].capitalize()[:3], s["time"])
                for s in ev.get("schedule", [])
            )
            embed.add_field(
                name="{} (`{}`)".format(ev.get("name", key), key),
                value="{}\nSchedule: {}".format(status, days_str),
                inline=False,
            )
        embed.set_footer(text="Use /event status <key> for next-run details")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @event_group.command(name="enable", description="Enable a scheduled event")
    @app_commands.describe(event_key="The event key (see /event list)")
    @is_admin()
    async def event_enable(self, interaction: discord.Interaction, event_key: str):
        events = self.config.get("events", {})
        if event_key not in events:
            await interaction.response.send_message(
                "Unknown key `{}`. Use `/event list` to see valid keys.".format(event_key),
                ephemeral=True,
            )
            return
        events[event_key]["enabled"] = True
        _save_config(self.config)
        await self._log_admin_action(interaction, "ENABLED", event_key)
        embed = discord.Embed(
            title="\u2705 Event Enabled",
            description="**{}** will now send scheduled announcements.".format(
                events[event_key].get("name", event_key)
            ),
            color=Config.COLOR_SUCCESS,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @event_group.command(name="disable", description="Disable a scheduled event")
    @app_commands.describe(event_key="The event key (see /event list)")
    @is_admin()
    async def event_disable(self, interaction: discord.Interaction, event_key: str):
        events = self.config.get("events", {})
        if event_key not in events:
            await interaction.response.send_message(
                "Unknown key `{}`.".format(event_key), ephemeral=True
            )
            return
        events[event_key]["enabled"] = False
        _save_config(self.config)
        await self._log_admin_action(interaction, "DISABLED", event_key)
        embed = discord.Embed(
            title="\U0001f534 Event Disabled",
            description="**{}** will no longer send announcements.".format(
                events[event_key].get("name", event_key)
            ),
            color=Config.COLOR_WARNING,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @event_group.command(name="toggle", description="Toggle a scheduled event on/off")
    @app_commands.describe(event_key="The event key (see /event list)")
    @is_admin()
    async def event_toggle(self, interaction: discord.Interaction, event_key: str):
        events = self.config.get("events", {})
        if event_key not in events:
            await interaction.response.send_message(
                "Unknown key `{}`.".format(event_key), ephemeral=True
            )
            return
        new_state = not events[event_key].get("enabled", True)
        events[event_key]["enabled"] = new_state
        _save_config(self.config)
        await self._log_admin_action(
            interaction, "TOGGLED -> {}".format("ON" if new_state else "OFF"), event_key
        )
        status = "\U0001f7e2 Enabled" if new_state else "\U0001f534 Disabled"
        embed = discord.Embed(
            title="Event Toggled",
            description="**{}** is now {}".format(
                events[event_key].get("name", event_key), status
            ),
            color=Config.COLOR_SUCCESS if new_state else Config.COLOR_WARNING,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @event_group.command(name="status", description="Detailed info about a specific event")
    @app_commands.describe(event_key="The event key (see /event list)")
    @is_admin()
    async def event_status(self, interaction: discord.Interaction, event_key: str):
        events = self.config.get("events", {})
        if event_key not in events:
            await interaction.response.send_message(
                "Unknown key `{}`.".format(event_key), ephemeral=True
            )
            return
        ev       = events[event_key]
        status   = "\U0001f7e2 Enabled" if ev.get("enabled", True) else "\U0001f534 Disabled"
        days_str = "\n".join(
            "\u2022 {} at {}".format(s["day"].capitalize(), s["time"])
            for s in ev.get("schedule", [])
        )
        next_run = self._next_occurrence(ev.get("schedule", []))
        embed = discord.Embed(
            title="\U0001f4cb {}".format(ev.get("name", event_key)),
            color=Config.COLOR_INFO,
        )
        embed.add_field(name="Status",             value=status,                     inline=True)
        embed.add_field(name="Event Key",          value="`{}`".format(event_key),   inline=True)
        embed.add_field(name="Next Announcement",  value=next_run,                   inline=False)
        embed.add_field(name="Schedule",           value=days_str,                   inline=False)
        embed.add_field(
            name="Message Preview",
            value="```{}```".format(ev.get("message", "")[:300]),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @event_group.command(
        name="send",
        description="Send an event announcement immediately"
    )
    @app_commands.describe(
        event_key="The event key (see /event list)",
        event_time="Optional time to insert into the message e.g. 10:15 PM (for Guild Party)"
    )
    @is_admin()
    async def event_send(
        self,
        interaction: discord.Interaction,
        event_key: str,
        event_time: str = None,
    ):
        events = self.config.get("events", {})
        if event_key not in events:
            await interaction.response.send_message(
                "Unknown key `{}`.".format(event_key), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Build the message — replace {time} placeholder if event_time provided
        event_data = dict(events[event_key])   # shallow copy so we don't mutate config
        msg = event_data.get("message", "")
        if event_time and "{time}" in msg:
            msg = msg.replace("{time}", event_time)
        elif "{time}" in msg and not event_time:
            # time placeholder present but none supplied — ask for it
            await interaction.followup.send(
                embed=discord.Embed(
                    title="\u26a0\ufe0f Time Required",
                    description=(
                        "This event message contains a `{{time}}` placeholder.\n"
                        "Please provide the `event_time` parameter.\n"
                        "Example: `/event send guild_party event_time:10:15 PM`"
                    ),
                    color=Config.COLOR_WARNING,
                ),
                ephemeral=True,
            )
            return
        event_data["message"] = msg

        channel_id = self.config.get("channel_id", "")
        await self._send_announcement(event_key, event_data, channel_id)
        await self._log_admin_action(
            interaction,
            "MANUAL SEND" + (" @ {}".format(event_time) if event_time else ""),
            event_key,
        )

        embed = discord.Embed(
            title="\u2705 Announcement Sent",
            description="**{}** sent to <#{}>{}".format(
                events[event_key].get("name", event_key),
                channel_id,
                "\nTime: **{}**".format(event_time) if event_time else "",
            ),
            color=Config.COLOR_SUCCESS,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @event_group.command(name="reload", description="Reload event config from file")
    @is_admin()
    async def event_reload(self, interaction: discord.Interaction):
        try:
            self.config = _load_config()
            self.tz = ZoneInfo(self.config.get("timezone", "Asia/Dhaka"))
            await self._log_admin_action(interaction, "RELOADED CONFIG", "all")
            embed = discord.Embed(
                title="\u2705 Configuration Reloaded",
                description="Loaded **{}** events from `events_config.json`".format(
                    len(self.config.get("events", {}))
                ),
                color=Config.COLOR_SUCCESS,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="\u274c Reload Failed",
                    description="Error: `{}`".format(str(e)),
                    color=Config.COLOR_ERROR,
                ),
                ephemeral=True,
            )

    # ── Autocomplete ──────────────────────────────────────────

    @event_enable.autocomplete("event_key")
    @event_disable.autocomplete("event_key")
    @event_toggle.autocomplete("event_key")
    @event_status.autocomplete("event_key")
    @event_send.autocomplete("event_key")
    async def event_key_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        events  = self.config.get("events", {})
        choices = []
        for key, ev in events.items():
            label = "{} ({})".format(ev.get("name", key), key)
            if current.lower() in label.lower() or current.lower() in key.lower():
                choices.append(app_commands.Choice(name=label[:100], value=key))
        return choices[:25]


async def setup(bot):
    await bot.add_cog(ScheduledEvents(bot))
