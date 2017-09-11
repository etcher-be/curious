"""
Misc utilities used in commands related things.
"""
import collections
import inspect
import re
from typing import Callable, Iterable, List, Union

from curious.commands.exc import MissingArgumentError
from curious.core.client import Client
from curious.dataclasses.message import Message
from curious.util import replace_quotes


async def _convert(ctx, tokens: List[str], signature: inspect.Signature):
    """
    Converts tokens passed from discord, using a signature.
    """
    final_args = []
    final_kwargs = {}

    args_it = iter(tokens)
    for n, (name, param) in enumerate(signature.parameters.items()):
        if n == 0:
            # Don't convert the `ctx` argument.
            continue

        assert isinstance(param, inspect.Parameter)
        # We loop over the signature parameters because it's easier to use those to consume.
        # Get the next argument from args.
        try:
            arg = next(args_it)
        except StopIteration as e:
            # not good!
            # If we're a *arg format, we can safely handle this, or if we have a default.
            if param.kind == inspect.Parameter.VAR_POSITIONAL:
                break

            if param.default is inspect.Parameter.empty:
                raise MissingArgumentError(ctx, param.name) from e

            arg = param.default

        # Begin the consumption!
        if param.kind in [inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          inspect.Parameter.POSITIONAL_ONLY]:
            # Only add it to the final_args, then continue the loop.
            arg = replace_quotes(arg)
            converter = ctx._lookup_converter(param.annotation)
            final_args.append(converter(ctx, arg))
            continue

        if param.kind in [inspect.Parameter.KEYWORD_ONLY]:
            # Only add it to final_kwargs.
            # This is a consume all operation, so we eat all of the arguments.
            f = [arg]

            while True:
                try:
                    next_arg = next(args_it)
                except StopIteration:
                    break

                f.append(next_arg)

            converter = ctx._lookup_converter(param.annotation)
            if len(f) == 1:
                final_kwargs[param.name] = converter(ctx, f[0])
            else:
                final_kwargs[param.name] = converter(ctx, " ".join(f))
            continue

        if param.kind in [inspect.Parameter.VAR_POSITIONAL]:
            # This *shouldn't* be called on `*` arguments, but we can't be sure.
            # Special case - consume ALL the arguments.
            f = [arg]

            while True:
                try:
                    next_arg = next(args_it)
                except StopIteration:
                    break

                f.append(next_arg)

            converter = ctx._lookup_converter(param.annotation)
            final_args.append(converter(ctx, " ".join(f)))

        if param.kind in [inspect.Parameter.VAR_KEYWORD]:
            # no
            continue

    return final_args, final_kwargs


def get_description(func) -> str:
    """
    Gets the description of a function.

    :param func: The function.
    :return: The description extracted from the docstring, or None.
    """
    if not func.__doc__:
        return None

    doc = inspect.cleandoc(func.__doc__)
    lines = doc.split("\n")
    return lines[0]


def split_message_content(content: str, delim: str = " ") -> List[str]:
    """
    Splits a message into individual parts by `delim`, returning a list of strings.
    This method preserves quotes.

    .. code-block:: python3

        content = '!send "Fuyukai desu" "Hello, world!"'
        split = split_message_content(content, delim=" ")

    :param content: The message content to split.
    :param delim: The delimiter to split on.
    :return: A list of items split
    """

    def replacer(m):
        return m.group(0).replace(delim, "\x00")

    parts = re.sub(r'".+?"', replacer, content).split()
    parts = [p.replace("\x00", " ") for p in parts]
    return parts


def prefix_check_factory(prefix: Union[str, Iterable[str], Callable[[Client, Message], str]]):
    """
    The default message function factory.

    This provides a callable that will fire a command if the message begins with the specified
    prefix or list of prefixes.

    If ``command_prefix`` is provided to the :class:`.Client`, then it will automatically call this
    function to get a message check function to use.

    .. code-block:: python3

        # verbose form
        message_check = prefix_check_factory(["!", "?"])
        cl = Client(message_check=message_check)

        # implicit form
        cl = Client(command_prefix=["!", "?"])

    The :attr:`prefix` is set on the returned function that can be used to retrieve the prefixes
    defined to create  the function at any time.

    :param prefix: A :class:`str` or :class:`typing.Iterable[str]` that represents the prefix(es) \
        to use.
    :return: A callable that can be used for the ``message_check`` function on the client.
    """

    async def __inner(bot: Client, message: Message):
        # move prefix out of global scope
        _prefix = prefix
        matched = None

        if callable(_prefix):
            _prefix = _prefix(bot, message)
            if inspect.isawaitable(_prefix):
                _prefix = await _prefix

        if isinstance(_prefix, str):
            match = message.content.startswith(_prefix)
            if match:
                matched = _prefix

        elif isinstance(prefix, collections.Iterable):
            for i in _prefix:
                if message.content.startswith(i):
                    matched = i
                    break

        if not matched:
            return None

        tokens = split_message_content(message.content[len(matched):])
        command_word = tokens[0]

        return command_word, tokens[1:]

    __inner.prefix = prefix
    return __inner
