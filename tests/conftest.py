import pytest

from src.services.teltonika import TeltonikaClient


@pytest.fixture(autouse=True)
def _no_modem_retry_delay(monkeypatch):
    """The real retry pause (1 s) only slows tests down."""
    monkeypatch.setattr(TeltonikaClient, "_RETRY_DELAY", 0)
