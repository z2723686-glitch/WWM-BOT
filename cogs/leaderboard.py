from utils.permissions import in_level_channel
"""
WHERE WINDS MEET - Leaderboard Cog
=====================================
Displays top users with XP and level info.
Features pagination via interactive buttons.
"""

import logging
import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from utils.xp_system import format_xp, calculate_level_from_xp

logger = logging.getLogger("wwm.leaderboard")

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


class LeaderboardView(discord.ui.View):
    """Paginated leaderboard view with navigation buttons."""

    def __init__(self, bot, guild: discord.Guild, total_users: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.guild = guild
        self.page = 0
        self.page_size = Config.LEADERBOARD_PAGE_SIZE
        self.total_users = total_users
        self.max_pages = max(1, (total_users - 1) // self.page_size)

    async def build_embed(self) -> discord.Embed:
        """Build the leaderboard embed for current page."""
        users = await self.bot.db.get_leaderboard(
            str(self.guild.id),
            page=self.page,
            limit=self.page_size
        )

        embed = discord.Embed(
            title=f"🏆 {self.guild.name} Leaderboard",
            color=Config.COLOR_PRIMARY
        )
        embed.set_thumbnail(url=self.guild.icon.url if self.guild.icon else None)

        if not users:
            embed.description = "No users found. Start chatting to earn XP!"
            return embed

        lines = []
        start_rank = self.page * self.page_size + 1

        for i, user_data in enumerate(users):
            rank = start_rank + i
            medal = MEDALS.get(rank, f"`#{rank:02d}`")

            # Try to get member name
            member = self.guild.get_member(int(user_data["user_id"]))
            name = member.name if member else f"User {user_data['user_id'][:6]}"

            total_xp = user_data.get("xp", 0)
            level, _, _ = calculate_level_from_xp(total_xp)

            lines.append(
                f"{medal} **{name}**\n"
                f"    Level **{level}** • {format_xp(total_xp)} XP"
            )

        embed.description = "\n\n".join(lines)
        embed.set_footer(
            text=f"Page {self.page + 1}/{self.max_pages + 1} • "
                 f"{self.total_users} members ranked • Where Winds Meet"
        )
        return embed

    async def update_buttons(self):
        """Enable/disable navigation buttons based on current page."""
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.max_pages

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self.update_buttons()
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_pages, self.page + 1)
        await self.update_buttons()
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        """Disable buttons on timeout."""
        for child in self.children:
            child.disabled = True


class Leaderboard(commands.Cog):
    """Leaderboard commands and display."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="leaderboard", description="View the server's top ranked members")
    @in_level_channel()
    async def leaderboard(self, interaction: discord.Interaction):
        """Display paginated leaderboard for the server."""
        await interaction.response.defer(thinking=True)

        guild_id = str(interaction.guild.id)

        # Count total ranked users
        total_users = await self.bot.db.users.count_documents(
            {"guild_id": guild_id, "xp": {"$gt": 0}}
        )

        if total_users == 0:
            embed = discord.Embed(
                title="🏆 Leaderboard",
                description="No members have earned XP yet!\nStart chatting to appear on the leaderboard.",
                color=Config.COLOR_PRIMARY
            )
            await interaction.followup.send(embed=embed)
            return

        # Build paginated view
        view = LeaderboardView(self.bot, interaction.guild, total_users)
        await view.update_buttons()
        embed = await view.build_embed()

        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="top", description="Quick view of top 5 members")
    @in_level_channel()
    async def top(self, interaction: discord.Interaction):
        """Quick top 5 display without pagination."""
        guild_id = str(interaction.guild.id)

        users = await self.bot.db.get_leaderboard(guild_id, page=0, limit=5)

        embed = discord.Embed(
            title=f"⚡ Top 5 — {interaction.guild.name}",
            color=Config.COLOR_PRIMARY
        )

        if not users:
            embed.description = "No rankings yet. Start chatting!"
            await interaction.response.send_message(embed=embed)
            return

        lines = []
        for i, user_data in enumerate(users):
            rank = i + 1
            medal = MEDALS.get(rank, f"#{rank}")
            member = interaction.guild.get_member(int(user_data["user_id"]))
            name = member.name if member else "Unknown"
            total_xp = user_data.get("xp", 0)
            level, _, _ = calculate_level_from_xp(total_xp)
            lines.append(f"{medal} **{name}** — Lv.{level} ({format_xp(total_xp)} XP)")

        embed.description = "\n".join(lines)
        embed.set_footer(text="Use /leaderboard for full rankings")
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Leaderboard(bot))
