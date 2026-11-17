# -*- coding: utf-8 -*-
"""
Test bootstrap.

* pymongo / motor: a tiny stand-in is installed ONLY if the real package is missing
  (the fake database works with either).
* discord.py: the stand-in below is ALWAYS used, even when the real library is installed,
  because the fake guilds/members/interactions subclass its simple placeholder classes.
  This keeps the suite runnable anywhere, but it also means these tests never exercise the
  real discord.py - that has to be verified on a staging server.

The discord stand-in mimics just enough of discord.py 2.x (Embed, ui.View/button,
app_commands Group/Command/describe/choices/check/Range/Choice, ext.commands Cog)
to import and exercise cogs/referral.py. It does not talk to Discord.
"""

import os
import sys
import types
from enum import Enum

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _missing(name):
    try:
        __import__(name)
        return False
    except ImportError:
        return True


def _install_pymongo():
    pm = types.ModuleType("pymongo")
    pm.ASCENDING, pm.DESCENDING = 1, -1

    class IndexModel:
        def __init__(self, keys, **kwargs):
            self.document = dict(kwargs, key=list(keys))

    class ReturnDocument:
        BEFORE, AFTER = False, True

    errors = types.ModuleType("pymongo.errors")

    class PyMongoError(Exception):
        pass

    class DuplicateKeyError(PyMongoError):
        pass

    class OperationFailure(PyMongoError):
        pass

    errors.PyMongoError, errors.DuplicateKeyError, errors.OperationFailure = (
        PyMongoError, DuplicateKeyError, OperationFailure)
    pm.IndexModel, pm.ReturnDocument, pm.errors = IndexModel, ReturnDocument, errors
    sys.modules["pymongo"], sys.modules["pymongo.errors"] = pm, errors

    motor = types.ModuleType("motor")
    ma = types.ModuleType("motor.motor_asyncio")
    ma.AsyncIOMotorClient = object
    sys.modules["motor"], sys.modules["motor.motor_asyncio"] = motor, ma


def _install_discord():
    d = types.ModuleType("discord")

    class DiscordException(Exception):
        pass

    class HTTPException(DiscordException):
        pass

    class Forbidden(HTTPException):
        pass

    class NotFound(HTTPException):
        pass

    class Embed:
        def __init__(self, title=None, description=None, color=None, **kw):
            self.title, self.description, self.color = title, description, color
            self.fields, self.footer = [], None

        def add_field(self, *, name, value, inline=True):
            self.fields.append({"name": name, "value": value, "inline": inline})
            return self

        def set_footer(self, *, text=None, **kw):
            self.footer = text
            return self

        def set_thumbnail(self, **kw):
            return self

    class _Plain:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Member(_Plain):
        pass

    class Role(_Plain):
        pass

    class Guild(_Plain):
        pass

    class Invite(_Plain):
        pass

    class Message(_Plain):
        pass

    class Interaction(_Plain):
        pass

    class TextChannel(_Plain):
        pass

    class MessageType(Enum):
        default = 0
        reply = 19
        join = 7

    class ButtonStyle(Enum):
        primary = 1
        secondary = 2
        success = 3
        danger = 4
        link = 5

    class AllowedMentions:
        @classmethod
        def none(cls):
            return cls()

    utils = types.ModuleType("discord.utils")
    utils.format_dt = lambda dt, style="f": "<t:{}:{}>".format(int(dt.timestamp()), style)
    d.utils = utils

    # ui
    ui = types.ModuleType("discord.ui")

    class View:
        def __init__(self, *, timeout=180.0):
            self.timeout = timeout

    class Button:
        pass

    def button(**meta):
        def deco(func):
            func.__button_meta__ = meta
            return func
        return deco

    ui.View, ui.Button, ui.button = View, Button, button
    d.ui = ui

    # app_commands
    ac = types.ModuleType("discord.app_commands")

    class Command:
        def __init__(self, callback, name, description, parent=None):
            self.callback, self.name, self.description, self.parent = callback, name, description, parent
            self.autocompletes = {}

        def autocomplete(self, param):
            def deco(fn):
                self.autocompletes[param] = fn
                return fn
            return deco

    class Group:
        def __init__(self, *, name, description, guild_only=False, **kw):
            self.name, self.description, self.guild_only, self.commands = name, description, guild_only, []

        def command(self, *, name, description):
            def deco(fn):
                cmd = Command(fn, name, description, parent=self)
                self.commands.append(cmd)
                return cmd
            return deco

    def command(*, name, description):
        def deco(fn):
            return Command(fn, name, description)
        return deco

    def describe(**kw):
        def deco(fn):
            fn.__describe__ = kw
            return fn
        return deco

    def choices(**kw):
        def deco(fn):
            fn.__choices__ = kw
            return fn
        return deco

    def check(pred):
        def deco(fn):
            fn.__checks__ = getattr(fn, "__checks__", []) + [pred]
            return fn
        return deco

    def guild_only():
        return lambda fn: fn

    class Choice:
        def __init__(self, *, name, value):
            self.name, self.value = name, value

        def __class_getitem__(cls, item):
            return cls

    class Range:
        def __class_getitem__(cls, item):
            return item[0]

    ac.Command, ac.Group, ac.command, ac.describe, ac.choices = Command, Group, command, describe, choices
    ac.check, ac.guild_only, ac.Choice, ac.Range = check, guild_only, Choice, Range
    d.app_commands = ac

    # ext.commands
    ext = types.ModuleType("discord.ext")
    cmds = types.ModuleType("discord.ext.commands")

    class Cog:
        @staticmethod
        def listener(name=None):
            def deco(fn):
                fn.__cog_listener__ = True
                return fn
            return deco

    class Bot:
        pass

    cmds.Cog, cmds.Bot = Cog, Bot
    ext.commands = cmds
    d.ext = ext

    for k, v in dict(DiscordException=DiscordException, HTTPException=HTTPException, Forbidden=Forbidden,
                     NotFound=NotFound, Embed=Embed, Member=Member, Role=Role, Guild=Guild, Invite=Invite,
                     Message=Message, Interaction=Interaction, TextChannel=TextChannel,
                     MessageType=MessageType, ButtonStyle=ButtonStyle, AllowedMentions=AllowedMentions).items():
        setattr(d, k, v)
    sys.modules.update({
        "discord": d, "discord.utils": utils, "discord.ui": ui, "discord.app_commands": ac,
        "discord.ext": ext, "discord.ext.commands": cmds,
    })


if _missing("pymongo"):
    _install_pymongo()
_install_discord()
