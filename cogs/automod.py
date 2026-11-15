# -*- coding: utf-8 -*-
"""
WWM - AutoMod Cog (Slang & Link Moderation)
===========================================
* Only acts in channels an admin has enabled with /automod enable-channel
* Per channel: filter slang, links, or both
* Admins/moderators (Manage Messages) and the bot owner are never filtered
* All settings live in MongoDB (collection: automod_settings) and survive restarts

Admin commands (all under /automod, Administrator only):
  enable-channel, disable-channel, channels, status,
  whitelist-add, whitelist-remove, whitelist-list,
  slang-add, slang-remove, slang-list, warn-toggle
"""

import logging
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from utils.permissions import is_admin, _is_owner, create_error_embed, create_success_embed
from utils.automod import (
    blocked_links,
    build_slang_patterns,
    find_slang,
    load_default_slang,
    normalize_domain,
)

logger = logging.getLogger("wwm.automod")

CACHE_SECONDS = 60          # safety net; the cache is also cleared on every change
NOTICE_SECONDS = 8          # how long the "message removed" notice stays


class AutoMod(commands.Cog):
    """Slang & link moderation for admin-selected channels."""

    def __init__(self, bot):
        self.bot = bot
        self._default_slang = load_default_slang()
        self._cache = {}    # guild_id -> (expires_at, compiled_config)

    automod = app_commands.Group(
        name="automod",
        description="Slang & link moderation settings",
        guild_only=True,
    )

    # ─────────────────────────────────────────
    # CONFIG LOADING (cached)
    # ─────────────────────────────────────────

    def _compile(self, doc: dict) -> dict:
        removed = set(doc.get("removed_slang", []))
        words = [w for w in self._default_slang if w not in removed]
        words += [w for w in doc.get("custom_slang", []) if w not in words]
        return {
            "channels":  doc.get("channels", {}) or {},
            "whitelist": list(doc.get("whitelist", [])),
            "words":     words,
            "patterns":  build_slang_patterns(words),
            "warn":      bool(doc.get("warn_on_violation", False)),
        }

    async def _get_cfg(self, guild_id: str) -> dict:
        hit = self._cache.get(guild_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        doc = await self.bot.db.get_automod_settings(guild_id)
        cfg = self._compile(doc)
        self._cache[guild_id] = (time.monotonic() + CACHE_SECONDS, cfg)
        return cfg

    async def _update(self, guild_id: str, ops: dict):
        await self.bot.db.update_automod_settings(guild_id, ops)
        self._cache.pop(guild_id, None)

    # ─────────────────────────────────────────
    # MESSAGE FILTERING
    # ─────────────────────────────────────────

    @staticmethod
    def _is_exempt(member) -> bool:
        if _is_owner(member.id):
            return True
        perms = getattr(member, "guild_permissions", None)
        return bool(perms and (perms.administrator or perms.manage_messages))

    async def _check_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        gid = str(message.guild.id)
        cfg = await self._get_cfg(gid)
        if not cfg["channels"]:
            return

        # threads inherit the rule of their parent channel
        rule = cfg["channels"].get(str(message.channel.id))
        if rule is None:
            parent_id = getattr(message.channel, "parent_id", None)
            if parent_id:
                rule = cfg["channels"].get(str(parent_id))
        if not rule or self._is_exempt(message.author):
            return

        reasons = []
        if rule.get("slang"):
            hits = find_slang(message.content, cfg["patterns"])
            if hits:
                reasons.append(("Slang", ", ".join(hits[:5])))
        if rule.get("links"):
            bad = blocked_links(message.content, cfg["whitelist"])
            if bad:
                reasons.append(("Link", ", ".join(bad[:5])))
        if not reasons:
            return

        await self._punish(message, reasons, cfg)

    async def _punish(self, message: discord.Message, reasons: list, cfg: dict):
        kinds = " + ".join(k for k, _ in reasons)
        deleted = True
        try:
            await message.delete()
        except discord.NotFound:
            return                          # someone else already removed it
        except discord.Forbidden:
            deleted = False
            logger.warning("AutoMod: missing 'Manage Messages' in #%s (%s)",
                           message.channel, message.guild.id)
        except discord.HTTPException as e:
            deleted = False
            logger.error("AutoMod: delete failed: %s", e)

        if deleted:
            try:
                await message.channel.send(
                    "{} your message was removed: **{}** is not allowed in this channel."
                    .format(message.author.mention, kinds.lower()),
                    delete_after=NOTICE_SECONDS,
                    allowed_mentions=discord.AllowedMentions(users=[message.author]),
                )
            except discord.HTTPException:
                pass

        # optional warning, stored with the existing /warn system
        warn_count = None
        if cfg["warn"]:
            try:
                warn_count = await self.bot.db.add_warning(
                    str(message.guild.id), str(message.author.id),
                    str(self.bot.user.id), "AutoMod: " + kinds,
                )
            except Exception as e:
                logger.error("AutoMod: could not store warning: %s", e)

        await self._log(message, reasons, deleted, warn_count)

    async def _log(self, message, reasons, deleted, warn_count):
        try:
            settings = await self.bot.db.get_guild_settings(str(message.guild.id))
            ch_id = settings.get("log_channel")
            channel = message.guild.get_channel(int(ch_id)) if ch_id else None
            if not channel:
                return
            embed = discord.Embed(
                title="🛡️ AutoMod — message {}".format("removed" if deleted else "flagged"),
                color=Config.COLOR_WARNING,
                timestamp=datetime.utcnow(),
            )
            embed.add_field(name="👤 User",
                            value="{} (`{}`)".format(message.author.mention, message.author.id))
            embed.add_field(name="📍 Channel", value=message.channel.mention)
            for kind, detail in reasons:
                embed.add_field(name="🚩 " + kind, value="`{}`".format(detail[:200]), inline=False)
            embed.add_field(name="💬 Content",
                            value=discord.utils.escape_markdown(message.content)[:900] or "—",
                            inline=False)
            if warn_count is not None:
                embed.add_field(name="⚠️ Total warnings", value=str(warn_count))
            if not deleted:
                embed.add_field(name="❗ Note",
                                value="Could not delete — give the bot **Manage Messages**.",
                                inline=False)
            embed.set_footer(text="Where Winds Meet • AutoMod")
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            logger.error("AutoMod: log failed: %s", e)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            await self._check_message(message)
        except Exception:
            logger.exception("AutoMod on_message error")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content == after.content:
            return
        try:
            await self._check_message(after)
        except Exception:
            logger.exception("AutoMod on_message_edit error")

    # ─────────────────────────────────────────
    # /automod — CHANNELS
    # ─────────────────────────────────────────

    @automod.command(name="enable-channel", description="Turn filtering on in a channel")
    @app_commands.describe(channel="Channel to protect", filter="What to filter there")
    @app_commands.choices(filter=[
        app_commands.Choice(name="Slang only", value="slang"),
        app_commands.Choice(name="Links only", value="links"),
        app_commands.Choice(name="Slang + Links", value="both"),
    ])
    @is_admin()
    async def enable_channel(self, interaction: discord.Interaction,
                             channel: discord.TextChannel,
                             filter: app_commands.Choice[str]):
        rule = {"slang": filter.value in ("slang", "both"),
                "links": filter.value in ("links", "both")}
        await self._update(str(interaction.guild.id),
                           {"$set": {"channels.{}".format(channel.id): rule}})
        await interaction.response.send_message(embed=create_success_embed(
            "AutoMod Enabled",
            "{} → **{}** filtering is now active.".format(channel.mention, filter.name)),
            ephemeral=True)

    @automod.command(name="disable-channel", description="Turn filtering off in a channel")
    @app_commands.describe(channel="Channel to stop filtering")
    @is_admin()
    async def disable_channel(self, interaction: discord.Interaction,
                              channel: discord.TextChannel):
        await self._update(str(interaction.guild.id),
                           {"$unset": {"channels.{}".format(channel.id): ""}})
        await interaction.response.send_message(embed=create_success_embed(
            "AutoMod Disabled", "{} is no longer filtered.".format(channel.mention)),
            ephemeral=True)

    @automod.command(name="channels", description="List channels with filtering enabled")
    @is_admin()
    async def list_channels(self, interaction: discord.Interaction):
        cfg = await self._get_cfg(str(interaction.guild.id))
        lines = []
        for cid, rule in cfg["channels"].items():
            ch = interaction.guild.get_channel(int(cid))
            what = " + ".join(n for n, on in (("slang", rule.get("slang")),
                                              ("links", rule.get("links"))) if on) or "nothing"
            lines.append("{} — {}".format(ch.mention if ch else "`deleted {}`".format(cid), what))
        await interaction.response.send_message(embed=discord.Embed(
            title="🛡️ AutoMod Channels",
            description="\n".join(lines) or "No channels configured yet. Use `/automod enable-channel`.",
            color=Config.COLOR_INFO), ephemeral=True)

    # ─────────────────────────────────────────
    # /automod — WHITELIST
    # ─────────────────────────────────────────

    @automod.command(name="whitelist-add", description="Allow a domain (subdomains included)")
    @app_commands.describe(domain="e.g. youtube.com")
    @is_admin()
    async def whitelist_add(self, interaction: discord.Interaction, domain: str):
        d = normalize_domain(domain)
        if "." not in d or " " in d:
            return await interaction.response.send_message(
                embed=create_error_embed("Invalid Domain", "Use a domain like `youtube.com`."),
                ephemeral=True)
        await self._update(str(interaction.guild.id), {"$addToSet": {"whitelist": d}})
        await interaction.response.send_message(embed=create_success_embed(
            "Domain Whitelisted", "`{}` and its subdomains are now allowed.".format(d)),
            ephemeral=True)

    @automod.command(name="whitelist-remove", description="Remove a whitelisted domain")
    @app_commands.describe(domain="e.g. youtube.com")
    @is_admin()
    async def whitelist_remove(self, interaction: discord.Interaction, domain: str):
        d = normalize_domain(domain)
        await self._update(str(interaction.guild.id), {"$pull": {"whitelist": d}})
        await interaction.response.send_message(embed=create_success_embed(
            "Whitelist Updated", "`{}` removed (if it was listed).".format(d)), ephemeral=True)

    @automod.command(name="whitelist-list", description="Show whitelisted domains")
    @is_admin()
    async def whitelist_list(self, interaction: discord.Interaction):
        cfg = await self._get_cfg(str(interaction.guild.id))
        text = "\n".join("• `{}`".format(d) for d in sorted(cfg["whitelist"])) or "No domains whitelisted."
        await interaction.response.send_message(embed=discord.Embed(
            title="🔗 Whitelisted Domains", description=text[:4000], color=Config.COLOR_INFO),
            ephemeral=True)

    # ─────────────────────────────────────────
    # /automod — SLANG LIST
    # ─────────────────────────────────────────

    @automod.command(name="slang-add", description="Add a word to the slang filter")
    @app_commands.describe(word="Word or phrase to block (any language)")
    @is_admin()
    async def slang_add(self, interaction: discord.Interaction, word: str):
        w = word.strip().lower()
        if not (2 <= len(w) <= 40):
            return await interaction.response.send_message(
                embed=create_error_embed("Invalid Word", "Use 2–40 characters."), ephemeral=True)
        await self._update(str(interaction.guild.id), {
            "$addToSet": {"custom_slang": w}, "$pull": {"removed_slang": w}})
        await interaction.response.send_message(embed=create_success_embed(
            "Word Added", "The word was added to the slang filter."), ephemeral=True)

    @automod.command(name="slang-remove", description="Remove a word from the slang filter")
    @app_commands.describe(word="Word to allow again")
    @is_admin()
    async def slang_remove(self, interaction: discord.Interaction, word: str):
        w = word.strip().lower()
        ops = {"$pull": {"custom_slang": w}}
        if w in self._default_slang:
            ops["$addToSet"] = {"removed_slang": w}
        await self._update(str(interaction.guild.id), ops)
        await interaction.response.send_message(embed=create_success_embed(
            "Word Removed", "The word is no longer filtered."), ephemeral=True)

    @automod.command(name="slang-list", description="Show the active slang list (only you see it)")
    @is_admin()
    async def slang_list(self, interaction: discord.Interaction):
        cfg = await self._get_cfg(str(interaction.guild.id))
        text = ", ".join("||{}||".format(w) for w in cfg["words"]) or "No words configured."
        await interaction.response.send_message(embed=discord.Embed(
            title="🚫 Slang Filter ({} words)".format(len(cfg["words"])),
            description=text[:4000], color=Config.COLOR_INFO), ephemeral=True)


    # /automod — OPTIONS & STATUS

    @automod.command(name="warn-toggle", description="Also add a warning when someone is filtered")
    @app_commands.describe(enabled="True = record a warning for each violation")
    @is_admin()
    async def warn_toggle(self, interaction: discord.Interaction, enabled: bool):
        await self._update(str(interaction.guild.id), {"$set": {"warn_on_violation": enabled}})
        await interaction.response.send_message(embed=create_success_embed(
            "AutoMod Warnings", "Warnings on violation: **{}**".format("ON" if enabled else "OFF")),
            ephemeral=True)

    @automod.command(name="status", description="Overview of AutoMod settings")
    @is_admin()
    async def status(self, interaction: discord.Interaction):
        cfg = await self._get_cfg(str(interaction.guild.id))
        me = interaction.guild.me
        can_delete = me.guild_permissions.manage_messages
        embed = discord.Embed(title="🛡️ AutoMod Status", color=Config.COLOR_INFO)
        embed.add_field(name="Channels", value=str(len(cfg["channels"])))
        embed.add_field(name="Slang words", value=str(len(cfg["words"])))
        embed.add_field(name="Whitelisted domains", value=str(len(cfg["whitelist"])))
        embed.add_field(name="Warn on violation", value="ON" if cfg["warn"] else "OFF")
        embed.add_field(name="Bot can delete messages", value="✅" if can_delete else "❌ needs Manage Messages")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
