"""Tests for Free API Registry & RPC Failover."""

import asyncio
import pytest

from apex_common.free_apis import RPCFailover


class TestRPCFailover:
    @pytest.mark.asyncio
    async def test_round_robin(self):
        f = RPCFailover(["http://a", "http://b", "http://c"])
        urls = set()
        for _ in range(10):
            url = await f.get_url()
            urls.add(url)
        # Should use all endpoints
        assert len(urls) >= 2

    @pytest.mark.asyncio
    async def test_failure_deprioritizes(self):
        f = RPCFailover(["http://bad", "http://good"])
        # Hammer failures on "bad"
        for _ in range(10):
            await f.report_failure("http://bad")
        await f.report_success("http://good", latency_ms=50)

        # "good" should now be preferred
        counts = {"http://bad": 0, "http://good": 0}
        for _ in range(20):
            url = await f.get_url()
            counts[url] += 1
        assert counts["http://good"] > counts["http://bad"]

    @pytest.mark.asyncio
    async def test_all_failing_resets(self):
        f = RPCFailover(["http://a", "http://b"])
        for _ in range(10):
            await f.report_failure("http://a")
            await f.report_failure("http://b")
        # Should still return something (resets on all-failing)
        url = await f.get_url()
        assert url in ("http://a", "http://b")

    @pytest.mark.asyncio
    async def test_status(self):
        f = RPCFailover(["http://a", "http://b"])
        await f.get_url()
        status = await f.get_status()
        assert len(status) == 2
        assert all("url" in s and "failures" in s for s in status)

    @pytest.mark.asyncio
    async def test_latency_tracking(self):
        f = RPCFailover(["http://fast", "http://slow"])
        await f.report_success("http://fast", latency_ms=10)
        await f.report_success("http://slow", latency_ms=5000)
        # Fast should be strongly preferred
        counts = {"http://fast": 0, "http://slow": 0}
        for _ in range(30):
            url = await f.get_url()
            counts[url] += 1
        assert counts["http://fast"] > counts["http://slow"]
