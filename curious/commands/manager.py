# This file is part of curious.
#
# curious is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# curious is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with curious.  If not, see <http://www.gnu.org/licenses/>.

"""
Contains the class for the commands manager for a client.

.. currentmodule:: curious.commands.manager
"""
import sys
from collections import defaultdict

import anyio
import importlib
import inspect
import logging
import traceback
from functools import partial
from typing import Callable, Dict, Iterable, Tuple, Type, Union

from curious.commands.context import Context
from curious.commands.exc import CommandsError
from curious.commands.help import help_command
from curious.commands.plugin import Plugin
from curious.commands.ratelimit import RateLimiter
from curious.commands.utils import prefix_check_factory
from curious.core import client as md_client
from curious.core.event import EventContext, event
from curious.dataclasses.message import Message
from curious.util import Promise

logger = logging.getLogger("curious.commands.manager")


class CommandsManager(object):
    """
    A manager that handles commands for a client.

    First, you need to create the manager and attach it to a client:

    .. code-block:: python3

        # form 1, automatically register with the client
        manager = CommandsManager.with_client(bot)

        # form 2, manually register
        manager = CommandsManager(bot)
        manager.register_events()

    This is required to add the handler events to the client.

    Next, you need to register a message check handler. This is a callable that is called for
    every message to try and extract the command from a message, if it matches.
    By default, the manager provides an easy way to use a simple command prefix:

    .. code-block:: python3

        # at creation time
        manager = CommandsManager(bot, command_prefix="!")

    At this point, the command prefix will be available on the manager with either
    :attr:`.Manager.command_prefix` or :attr:`.Manager.message_check.prefix`.

    If you need more complex message checking, you can use ``message_check``:

    .. code-block:: python3

        manager = CommandsManager(bot, message_check=my_message_checker)
        # or
        manager.message_check = my_message_checker

    Finally, you can register plugins or modules containing plugins with the manager:

    .. code-block:: python3

        @bot.event("ready")
        async def load_plugins(ctx: EventContext):
            # load plugin explicitly
            await manager.load_plugin(PluginClass, arg1)
            # load plugins from a module
            await manager.load_plugins_from("my.plugin.module")

    You can also add free-standing commands that aren't bound to a plugin with
    :meth:`.CommandsManager.add_command`:

    .. code-block:: python3

        @command()
        async def ping(ctx: CommandsContext):
            await ctx.channel.messages.send(content="Ping!")

        manager.add_command(ping)

    These will then be available to the client.
    """

    def __init__(self, client: 'md_client.Client', *,
                 message_check=None,
                 command_prefix: Union[str, Iterable[str],
                                       Callable[[md_client.Client, Message], str]] = None,
                 context_klass: Type[Context] = Context):
        """
        :param client: The :class:`.Client` to use with this manager.
        :param message_check: The message check function for this manager.

            This should take two arguments, the client and message, and should return either None
            or a 2-item tuple:
              - The command word matched
              - The tokens after the command word

        :param command_prefix: The command prefix, if no message check is provided.
        """
        if message_check is None and command_prefix is None:
            raise ValueError("Must provide one of message_check or command_prefix")

        #: The client for this manager.
        self.client = client

        if message_check is None:
            message_check = prefix_check_factory(command_prefix)

        #: The message check function for this manager.
        self.message_check = message_check

        #: A dictionary mapping of <plugin name> -> <plugin> object.
        self.plugins: Dict[str, Tuple[Plugin, anyio.CancelScope]] = {}

        #: A dictionary mapping of <plugin> -> <dict of commands> object for cache purposes.
        self._plugin_command_cache = {}

        #: A dictionary of stand-alone commands, i.e. commands not associated with a plugin.
        self.commands = {}

        #: The context class for this context.
        self.context_class = context_klass

        #: The current ratelimiter.
        self.ratelimiter = RateLimiter()

        self._module_plugins = defaultdict(lambda: [])

    @classmethod
    def with_client(cls, client: 'md_client.Client', **kwargs):
        """
        Creates a manager and automatically registers events.
        """
        obb = cls(client=client, **kwargs)
        obb.register_events()
        return obb

    def register_events(self) -> None:
        """
        Copies the events to the client specified on this manager.
        """
        self.client.events.add_event(self.handle_message)
        self.client.events.add_event(self.default_command_error)
        self.client.events.add_event_hook(self.event_hook)

        from curious.commands.decorators import command
        self.commands["help"] = command(name="help")(help_command)

    async def load_plugin(self, klass: Type[Plugin], *args,
                          module: str = None):
        """
        Loads a plugin.

        .. note::

            The client instance will automatically be provided to the Plugin's ``__init__``.

        :param klass: The plugin class to load.
        :param args: Any args to provide to the plugin.
        :param module: The module name provided with this plugin. Only used interally.
        """
        # get the name and create the plugin object
        plugin_name = getattr(klass, "plugin_name", klass.__name__)
        instance = klass(*args)

        # call load, of course
        await instance.plugin_load()

        # set up a cancel scope which allows "easy" background task management and
        # automatic cancellation of them
        p_scope = Promise()

        async def _run_plugin():
            async with anyio.open_cancel_scope() as scope:
                await p_scope.set(scope)
                await instance.plugin_run()

        await self.client._spawn_task_internal(_run_plugin)
        new_scope = await p_scope.wait()

        self.plugins[plugin_name] = (instance, new_scope)
        if module is not None:
            self._module_plugins[module].append(instance)

        commands = instance._get_commands()
        table = {}
        for command in commands:
            table[command.cmd_name] = command
            for alias in command.cmd_aliases:
                table[alias] = command

        self._plugin_command_cache[instance] = table

        return instance

    async def unload_plugin(self, klass: Union[Type[Plugin], str]):
        """
        Unloads a plugin.

        :param klass: The plugin class or name of plugin to unload.
        """
        plugin, scope = (None, None)
        if isinstance(klass, str):
            plugin, scope = self.plugins.pop(klass)
        else:
            for k, (s, p) in self.plugins.copy().items():
                if type(p) == klass:
                    plugin, scope = self.plugins.pop(k)
                    break

        if plugin is None:
            return

        # cancel any old bg tasks
        await scope.cancel()

        if plugin is not None:
            await plugin.plugin_unload()
            self._plugin_command_cache.pop(getattr(plugin, "plugin_name", type(plugin).__name__))

        return plugin

    def lookup_command(self, name: str):
        """
        Does a lookup in plugin and standalone commands.
        """
        if name in self.commands:
            return self.commands[name]

        for plugin, commands in self._plugin_command_cache.items():
            if name in commands:
                return commands[name]

    def get_command(self, command_name: str):
        """
        Gets a command from the internal command storage.
        If provided a string separated by spaces, a subcommand lookup will be attempted.

        :param command_name: The name of the command to lookup.
        """
        # do an immediate lookup for the first token
        sp = command_name.split(" ")
        command = self.lookup_command(sp[0])

        if command is None:
            return None

        for token in sp[1:]:
            try:
                filtered = filter(lambda cmd: cmd.cmd_name == token or token in cmd.cmd_aliases,
                                  command.cmd_subcommands)
                command = next(filtered)
            except StopIteration:
                return None

        return command

    def add_command(self, command):
        """
        Adds a command.

        :param command: A command function.
        """
        if not hasattr(command, "is_cmd"):
            raise ValueError("Commands must be decorated with the command decorator")

        self.commands[command.cmd_name] = command
        for alias in command.cmd_aliases:
            self.commands[alias] = command

        return command

    def remove_command(self, command):
        """
        Removes a command.

        :param command: The name of the command, or the command function.
        """
        if isinstance(command, str):
            return self.commands.pop(command)
        else:
            for k, p in self.commands.copy().items():
                if p == command:
                    return self.commands.pop(k)

    async def load_plugins_from(self, import_path: str):
        """
        Loads plugins from the specified module.

        :param import_path: The import path to import.
        """
        mod = importlib.import_module(import_path)

        # define the predicate for the body scanner
        def predicate(item):
            if not isinstance(item, type):
                return False

            # only accept plugin subclasses
            if not issubclass(item, Plugin):
                return False

            # ensure item is not actually Plugin
            if item == Plugin:
                return False

            # it is a plugin
            return True

        for plugin_name, plugin_class in inspect.getmembers(mod, predicate=predicate):
            await self.load_plugin(plugin_class, module=mod)

    async def unload_plugins_from(self, import_path: str):
        """
        Unloads plugins from the specified module.
        This will delete the module from sys.path.

        :param import_path: The import path.
        """
        # resolve string modules automatically
        if isinstance(import_path, str):
            module = sys.modules[import_path]
        else:
            module = import_path

        for plugin in self._module_plugins[module]:
            await plugin.plugin_unload()
            name = getattr(plugin, "plugin_name", type(plugin).__name__)
            self.plugins.pop(name)

        del sys.modules[import_path]
        del self._module_plugins[module]

    async def event_hook(self, ctx: EventContext, *args, **kwargs):
        """
        The event hook for the commands manager.
        """
        async with anyio.create_task_group() as tg:
            tg: anyio.TaskGroup
            for plugin in self.plugins.values():
                body = inspect.getmembers(plugin, predicate=lambda v: hasattr(v, "is_event"))
                for _, handler in body:
                    if ctx.event_name not in handler.events:
                        continue

                    cofunc = partial(self.client.events._safety_wrapper,
                                     handler, ctx, *args, **kwargs)

                    await tg.spawn(cofunc)

    async def handle_commands(self, ctx: EventContext, message: Message):
        """
        Handles commands for a message.
        """
        # don't process messages pre-cache
        if not message.author:
            return

        # check bot type
        if message.author.user.bot and self.client.bot_type & 8:
            return

        if message.author.user != self.client.user and self.client.bot_type & 64:
            return

        if message.guild_id is not None and self.client.bot_type & 32:
            return

        if message.guild_id is None and self.client.bot_type & 16:
            return

        # step 1, match the messages
        matched = self.message_check(self.client, message)
        if inspect.isawaitable(matched):
            matched = await matched

        if matched is None:
            return None

        # deconstruct the tuple returned into more useful variables than a single tuple
        command_word, tokens = matched

        # step 2, create the new commands context
        ctx = self.context_class(event_context=ctx, message=message)
        ctx.root_command_name = command_word
        ctx.full_tokens = tokens
        ctx.tokens = ctx.full_tokens
        ctx.manager = self

        # step 3, invoke the context to try and match the command and run it
        try:
            await ctx.run_contained_command()
        except CommandsError as e:
            reraise_ctx = ctx._make_reraise_ctx(e.event_name)
            await self.client.events.fire_event("command_error", e, ctx=reraise_ctx)
            await self.client.events.fire_event(e.event_name, e, ctx=reraise_ctx)

    @event("command_error")
    async def default_command_error(self, ctx: Context, err: CommandsError):
        """
        Handles command errors by default.
        """
        # autoremove ourself if applicable
        if len(self.client.events.event_listeners.getall("command_error")) > 1:
            self.client.events.remove_event("command_error", self.default_command_error)
            return

        fmtted = ''.join(traceback.format_exception(type(err), err, err.__traceback__))
        logger.error(f"Error in command!\n{fmtted}")

    @event("message_create")
    async def handle_message(self, ctx: EventContext, message: Message):
        """
        Registered as the event handler in a client for handling commands.
        """
        return await self.handle_commands(ctx, message)
