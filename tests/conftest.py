import socket

import pytest

from support import AP


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Every test runs with no key and no network. Any connection attempt fails the test."""
    for name in (
        'LLM_API_KEY',
        'DEEPSEEK_API_KEY',
        'DEEPSEEK_MODEL',
        'XAI_API_KEY',
        'LLM_BASE_URL',
        'LLM_MODEL',
        'VP_MODEL',
        'CRITIC_MODEL',
    ):
        monkeypatch.delenv(name, raising=False)
    attempts = []

    def refuse(sock, address, *args):
        attempts.append(address)
        raise ConnectionRefusedError('tests run offline')

    monkeypatch.setattr(socket.socket, 'connect', refuse)
    yield
    assert not attempts, f'a test tried to reach the network: {attempts}'


@pytest.fixture
def ap(tmp_path):
    return AP(tmp_path)
