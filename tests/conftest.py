"""Hermetic default test environment from SPEC §21."""

from __future__ import annotations

import os
import socket

import pytest


@pytest.fixture(autouse=True, scope="session")
def hermetic_session(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    patch = pytest.MonkeyPatch()
    for key in tuple(os.environ):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN") or key in {
            "OPENROUTER_MANAGEMENT_KEY",
            "ANTHROPIC_AUTH_TOKEN",
        }:
            patch.delenv(key, raising=False)

    def deny_network(*args, **kwargs):
        raise OSError("network access is disabled in the default test suite")

    patch.setattr(socket.socket, "connect", deny_network)
    patch.setattr(socket.socket, "connect_ex", deny_network)
    patch.setattr(socket, "create_connection", deny_network)
    patch.setenv("SIMMONS_AUTHOR", "test")
    patch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path_factory.mktemp("simmons-runs")))
    request.addfinalizer(patch.undo)

