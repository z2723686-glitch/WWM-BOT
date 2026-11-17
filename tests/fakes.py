# -*- coding: utf-8 -*-
"""Fake Discord objects (guilds, members, invites, interactions) for the tests."""

import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

_ids = itertools.count(1000)


def now_aware():
    return datetime.now(timezone.utc)


class FakeInvite:
    def __init__(self, code, inviter=None, uses=0, max_uses=0, guild=None, expires_at=None):
        self.code, self.inviter, self.uses, self.max_uses = code, inviter, uses, max_uses
        self.guild, self.expires_at = guild, expires_at
        self.url = "https://discord.gg/" + code


class FakeMember(discord.Member):
    def __init__(self, user_id=None, *, age_days=30, bot=False, guild=None, name=None):
        uid = user_id or next(_ids)
        super().__init__(id=uid, bot=bot, guild=guild, display_name=name or "user{}".format(uid), mention="<@%d>" % uid,
                         created_at=now_aware() - timedelta(days=age_days), roles=[],
                         guild_permissions=SimpleNamespace(administrator=False))

    def __str__(self):
        return self.display_name


class FakeChannel(discord.TextChannel):
    def __init__(self, guild, channel_id=None, can_invite=True):
        cid = channel_id or next(_ids)
        super().__init__(id=cid, guild=guild, can_invite=can_invite, parent_id=None, mention="<#%d>" % cid)
        self.sent = []

    def permissions_for(self, me):
        return SimpleNamespace(create_instant_invite=self.can_invite)

    async def create_invite(self, **kw):
        inv = FakeInvite("pc" + str(next(_ids)), inviter=self.guild.me, guild=self.guild)
        self.guild.live.append(inv)
        return inv

    async def send(self, **kw):
        self.sent.append(kw)


class FakeGuild(discord.Guild):
    def __init__(self, guild_id=1, name="Test Realm"):
        super().__init__(id=guild_id, name=name, features=[], members={})
        self.me = FakeMember(999, bot=True, guild=self)
        self.live = []                 # invites currently on the server
        self.forbidden = False
        self.vanity = None
        self.system_channel = None
        self.rules_channel = None
        self.channel = FakeChannel(self)
        self.text_channels = [self.channel]
        self.channels = {self.channel.id: self.channel}

    async def invites(self):
        if self.forbidden:
            raise discord.Forbidden()
        return list(self.live)

    async def vanity_invite(self):
        return self.vanity

    def get_member(self, uid):
        return self.members.get(uid)

    def get_channel(self, cid):
        return self.channels.get(cid)

    def add_member(self, **kw):
        m = FakeMember(guild=self, **kw)
        self.members[m.id] = m
        return m

    def add_invite(self, code, inviter=None, **kw):
        inv = FakeInvite(code, inviter=inviter, guild=self, **kw)
        self.live.append(inv)
        return inv


def make_interaction(guild, user, interaction_id=None):
    """A mocked slash-command / button interaction that records what the bot sends."""
    sent = []
    it = MagicMock()
    it.id = interaction_id or next(_ids)
    it.guild, it.user, it.channel = guild, user, guild.channel
    done = {"v": False}

    async def defer(**kw):
        done["v"] = True

    async def send_message(*a, **kw):
        done["v"] = True
        sent.append(kw)

    async def followup_send(*a, **kw):
        sent.append(kw)

    it.response.defer = AsyncMock(side_effect=defer)
    it.response.send_message = AsyncMock(side_effect=send_message)
    it.response.is_done = lambda: done["v"]
    it.followup.send = AsyncMock(side_effect=followup_send)
    it.sent = sent
    it.client = MagicMock()
    return it
