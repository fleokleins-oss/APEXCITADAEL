"""Tests for Apex Citadel v3 Circuit Breaker."""

import asyncio
import time

import pytest
from apex_common.circuit_breaker import CircuitBreakerRegistry, CBState


@pytest.fixture
def cb():
    return CircuitBreakerRegistry(failure_threshold=3, cooldown_s=0.5, probe_interval_s=0.2)


@pytest.mark.asyncio
async def test_starts_closed(cb):
    assert await cb.is_available("brain")
    status = await cb.get_status("brain")
    assert status["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_trips_after_threshold(cb):
    for _ in range(3):
        await cb.record_failure("brain")
    assert not await cb.is_available("brain")
    status = await cb.get_status("brain")
    assert status["state"] == "OPEN"


@pytest.mark.asyncio
async def test_success_resets_counter(cb):
    await cb.record_failure("brain")
    await cb.record_failure("brain")
    await cb.record_success("brain")
    # Counter reset, one more failure shouldn't trip
    await cb.record_failure("brain")
    assert await cb.is_available("brain")


@pytest.mark.asyncio
async def test_cooldown_allows_probe(cb):
    for _ in range(3):
        await cb.record_failure("brain")
    assert not await cb.is_available("brain")

    # Wait for cooldown
    await asyncio.sleep(0.6)
    # Should be HALF_OPEN, allowing probe
    assert await cb.is_available("brain")
    status = await cb.get_status("brain")
    assert status["state"] == "HALF_OPEN"


@pytest.mark.asyncio
async def test_probe_success_closes(cb):
    for _ in range(3):
        await cb.record_failure("brain")
    await asyncio.sleep(0.6)
    assert await cb.is_available("brain")  # HALF_OPEN

    await cb.record_success("brain")
    status = await cb.get_status("brain")
    assert status["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_probe_failure_reopens(cb):
    for _ in range(3):
        await cb.record_failure("brain")
    await asyncio.sleep(0.6)
    assert await cb.is_available("brain")  # HALF_OPEN

    await cb.record_failure("brain")
    status = await cb.get_status("brain")
    assert status["state"] == "OPEN"


@pytest.mark.asyncio
async def test_force_close(cb):
    for _ in range(3):
        await cb.record_failure("brain")
    await cb.force_close("brain")
    assert await cb.is_available("brain")
    status = await cb.get_status("brain")
    assert status["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_force_open(cb):
    await cb.force_open("brain")
    assert not await cb.is_available("brain")


@pytest.mark.asyncio
async def test_multiple_nodes_independent(cb):
    for _ in range(3):
        await cb.record_failure("brain")
    assert not await cb.is_available("brain")
    assert await cb.is_available("spoofhunter")


@pytest.mark.asyncio
async def test_get_all_status(cb):
    await cb.record_success("brain")
    await cb.record_failure("spoofhunter")
    statuses = await cb.get_all_status()
    assert len(statuses) == 2
    names = {s["node"] for s in statuses}
    assert "brain" in names
    assert "spoofhunter" in names
