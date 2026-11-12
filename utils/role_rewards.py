"""
WHERE WINDS MEET - Role Rewards
==================================
Handles automatic role assignment and removal based on user level.
Supports guild-specific role configurations.
"""

import logging
import discord
from typing import Optional

logger = logging.getLogger("wwm.role_rewards")


async def process_role_rewards(
    bot,
    member: discord.Member,
    old_level: int,
    new_level: int
) -> list[discord.Role]:
    """
    Process role rewards when a user levels up.
    Adds new roles and removes old ones based on guild config.
    
    Returns list of newly assigned roles.
    """
    if old_level == new_level:
        return []

    guild_id = str(member.guild.id)
    new_roles = []

    try:
        # Get all role rewards for this guild
        role_rewards = await bot.db.get_role_rewards(guild_id)
        if not role_rewards:
            return []

        for reward in role_rewards:
            reward_level = reward["level"]
            role_id = reward["role_id"]

            # Get the role object
            role = member.guild.get_role(int(role_id))
            if not role:
                continue  # Role was deleted, skip

            # User passed this level threshold
            if old_level < reward_level <= new_level:
                # Add the role if they don't have it
                if role not in member.roles:
                    try:
                        await member.add_roles(role, reason=f"Level {reward_level} reward")
                        new_roles.append(role)
                        logger.info(f"✅ Gave role {role.name} to {member} for level {reward_level}")
                    except discord.Forbidden:
                        logger.warning(f"⚠️ Can't assign role {role.name} - missing permissions")
                    except Exception as e:
                        logger.error(f"Error assigning role: {e}")

            # Remove roles from lower levels (progressive system)
            # Only remove if there's a higher role to replace it
            elif reward_level < new_level and role in member.roles:
                # Check if there's a higher level role they should have
                higher_rewards = [r for r in role_rewards if r["level"] > reward_level]
                if higher_rewards and any(
                    old_level < r["level"] <= new_level for r in higher_rewards
                ):
                    # User has leveled past this role, but we keep it (stacking roles)
                    # To REMOVE old roles, uncomment below:
                    # await member.remove_roles(role, reason="Level progression")
                    pass

    except Exception as e:
        logger.error(f"Error processing role rewards for {member}: {e}")

    return new_roles


async def get_next_role_reward(bot, guild_id: str, current_level: int) -> Optional[dict]:
    """Get the next role reward a user is working toward."""
    role_rewards = await bot.db.get_role_rewards(guild_id)
    
    for reward in sorted(role_rewards, key=lambda x: x["level"]):
        if reward["level"] > current_level:
            return reward
    
    return None  # Max level reached
