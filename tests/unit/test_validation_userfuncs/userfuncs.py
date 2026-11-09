"""Importable helper functions for deep-validation tests.

This directory is added to sys.path via the validator's ``--syspath`` option so
that ``userfuncs:<name>`` references resolve during deep validation tests.
"""

from pytest_httpchain import ProtocolAdapter


def good_auth():
    """An auth function callable with no arguments."""
    return None


def auth_with_required(token):
    """An auth function that requires an argument (auth is called with none)."""
    return token


def auth_posonly(token, /):
    """An auth function with a required positional-only parameter.

    The framework only ever passes arguments by keyword (and none at all for
    auth), so this can never be satisfied at runtime."""
    return token


def auth_posonly_with_kwargs(token, /, **kwargs):
    """A keyword named token lands in kwargs; it cannot fill token."""
    return token


def needs_response(response):
    """A save/verify function that accepts only the injected response."""
    return True


def needs_response_and_x(response, x):
    """A save/verify function that needs an extra required argument."""
    return bool(x)


def accepts_kwargs(response, **kwargs):
    """A function that accepts arbitrary keyword arguments."""
    return True


def needs_message(message):
    """A save/verify function for a connection stage: it gets message=."""
    return True


def not_an_adapter(**params):
    """A function named as a connection's adapter, which must be a class."""
    return None


class GoodAdapter:
    """An adapter whose methods take what the plugin passes, built with a
    required ``host`` and an optional ``port``."""

    def __init__(self, host, port=0):
        self.host, self.port = host, port

    def connect(self, timeout):
        return None

    def send(self, message):
        return None

    def receive(self, timeout):
        return None

    def close(self):
        pass


class StaticAndClassMethodAdapter:
    """Methods declared as a staticmethod and a classmethod: neither takes
    the instance, and both are callable as the plugin calls them."""

    @staticmethod
    def connect(timeout):
        return None

    @classmethod
    def send(cls, message):
        return None

    def receive(self, timeout, /):
        return None

    def close(self, *args):
        pass


class KwargsAdapter(GoodAdapter):
    """A constructor that takes any keyword argument, inheriting its methods."""

    def __init__(self, **options):
        self.options = options


class MissingCloseAdapter:
    """No close()."""

    def connect(self, timeout):
        return None

    def send(self, message):
        return None

    def receive(self, timeout):
        return None


class StubReceiveAdapter(ProtocolAdapter):
    """Subclasses the published Protocol and leaves receive() to its stub,
    which returns None forever."""

    def __init__(self):
        pass

    def connect(self, timeout):
        return None

    def send(self, message):
        return None

    def close(self):
        pass


class ProtocolNoInitAdapter(ProtocolAdapter):
    """Every method its own, but no __init__: typing's placeholder takes no
    arguments at all, whatever its signature says."""

    def connect(self, timeout):
        return None

    def send(self, message):
        return None

    def receive(self, timeout):
        return None

    def close(self):
        pass


class BadSendAdapter:
    """send() without the message the plugin passes it."""

    def connect(self, timeout):
        return None

    def send(self):
        return None

    def receive(self, timeout):
        return None

    def close(self):
        pass


class BadStaticReceiveAdapter:
    """A staticmethod receive() written as if it took the instance: the
    plugin's timeout fills self, and timeout is missing."""

    def connect(self, timeout):
        return None

    def send(self, message):
        return None

    @staticmethod
    def receive(self, timeout):
        return None

    def close(self):
        pass
