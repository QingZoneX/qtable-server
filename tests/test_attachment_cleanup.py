from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.services import attachment_cleanup


@pytest.mark.asyncio
async def test_cleanup_loop_honors_configured_interval(monkeypatch):
    cleanup_calls = 0
    sleep_calls: list[float] = []

    async def fake_cleanup_once() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(settings, "ATTACHMENT_CLEANUP_INTERVAL_SECONDS", 7.5)
    monkeypatch.setattr(attachment_cleanup, "_cleanup_once", fake_cleanup_once)
    monkeypatch.setattr(attachment_cleanup.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await attachment_cleanup._cleanup_loop()

    assert cleanup_calls == 1
    assert sleep_calls == [7.5]


@pytest.mark.asyncio
async def test_cleanup_loop_clamps_invalid_hot_loop_interval(monkeypatch):
    async def fake_cleanup_once() -> None:
        return None

    observed: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        observed.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(settings, "ATTACHMENT_CLEANUP_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(attachment_cleanup, "_cleanup_once", fake_cleanup_once)
    monkeypatch.setattr(attachment_cleanup.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await attachment_cleanup._cleanup_loop()

    assert observed == [0.1]
