"""Install the hermetic boundary before pytest imports test modules."""

from __future__ import annotations

import os
import socket
import shutil
import tempfile

import pytest

_PATCH: pytest.MonkeyPatch | None = None
_RUNS_ROOT: str | None = None


def _deny_network(*args, **kwargs):
    raise OSError("network access is disabled in the default test suite")


def _guard_socket_method(original):
    def guarded(sock, *args, **kwargs):
        if sock.family != socket.AF_UNIX:
            return _deny_network(*args, **kwargs)
        return original(sock, *args, **kwargs)

    return guarded


def _is_live_opt_in(config) -> bool:
    return config.option.markexpr.strip() == "live"


def pytest_configure(config: pytest.Config) -> None:
    """Scrub keys and block network I/O before test-module collection."""
    global _PATCH, _RUNS_ROOT
    if _is_live_opt_in(config):
        return

    patch = pytest.MonkeyPatch()
    for key in tuple(os.environ):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN") or key in {
            "OPENROUTER_MANAGEMENT_KEY",
            "ANTHROPIC_AUTH_TOKEN",
        }:
            patch.delenv(key, raising=False)
    patch.setenv("SIMMONS_DISABLE_DOTENV", "1")
    patch.setenv("SIMMONS_AUTHOR", "test")
    _RUNS_ROOT = tempfile.mkdtemp(prefix="simmons-runs-")
    patch.setenv("SIMMONS_RUNS_ROOT", _RUNS_ROOT)

    for method_name in (
        "accept",
        "accept_into",
        "bind",
        "connect",
        "connect_ex",
        "listen",
        "recv",
        "recv_into",
        "recvfrom",
        "recvfrom_into",
        "recvmsg",
        "recvmsg_into",
        "send",
        "sendall",
        "sendfile",
        "sendmsg",
        "sendto",
        "shutdown",
    ):
        if hasattr(socket.socket, method_name):
            original = getattr(socket.socket, method_name)
            patch.setattr(socket.socket, method_name, _guard_socket_method(original))

    for function_name in (
        "create_connection",
        "create_server",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "getnameinfo",
    ):
        if hasattr(socket, function_name):
            patch.setattr(socket, function_name, _deny_network)

    _PATCH = patch


def pytest_unconfigure(config: pytest.Config) -> None:
    global _PATCH, _RUNS_ROOT
    if _PATCH is not None:
        _PATCH.undo()
        _PATCH = None
    if _RUNS_ROOT is not None:
        shutil.rmtree(_RUNS_ROOT, ignore_errors=True)
        _RUNS_ROOT = None
