"""Tests cannot accidentally send production traffic or consume model credits."""
import socket

import pytest


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("External network is disabled in offline tests")
    monkeypatch.setattr(socket, "create_connection", blocked)
