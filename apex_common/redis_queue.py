from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Tuple

import redis.asyncio as redis

def _env(name: str, default: str) -> str:
    return os.getenv(name, default)

REDIS_URL = _env("REDIS_URL", "redis://127.0.0.1:6379/0")
STREAM = _env("MAESTRO_QUEUE_STREAM", "apex:maestro:jobs")
DLQ_STREAM = _env("MAESTRO_DLQ_STREAM", "apex:maestro:dlq")
GROUP = _env("MAESTRO_QUEUE_GROUP", "apex-maestro")
CONSUMER = _env("MAESTRO_QUEUE_CONSUMER", "worker-1")
JOB_TTL = int(_env("MAESTRO_JOB_TTL_SECONDS", "86400"))
JOB_MAX_ATTEMPTS = int(_env("MAESTRO_JOB_MAX_ATTEMPTS", "5"))

DELAYED_ZSET = _env("MAESTRO_DELAYED_ZSET", "apex:maestro:delayed")

def job_key(job_id: str) -> str:
    return f"apex:maestro:job:{job_id}"

async def get_redis() -> redis.Redis:
    return redis.from_url(REDIS_URL, decode_responses=True)

async def ensure_group(r: redis.Redis):
    try:
        await r.xgroup_create(STREAM, GROUP, id="0-0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise
    try:
        await r.xadd(DLQ_STREAM, {"boot": str(int(time.time()))}, maxlen=1, approximate=True)
    except Exception:
        pass

async def enqueue_job(r: redis.Redis, job_id: str, payload: Dict[str, Any]) -> Tuple[str, str]:
    now = int(time.time())
    existing = await r.hgetall(job_key(job_id))
    if existing and existing.get("status"):
        return "EXISTS", existing.get("msg_id", "")

    msg_id = await r.xadd(STREAM, {"job_id": job_id})
    await r.hset(job_key(job_id), mapping={
        "status": "QUEUED",
        "created_at": str(now),
        "payload": json.dumps(payload, ensure_ascii=False),
        "attempts": "0",
        "msg_id": msg_id,
    })
    await r.expire(job_key(job_id), JOB_TTL)
    return "ENQUEUED", msg_id

async def set_job_status(r: redis.Redis, job_id: str, status: str, **fields):
    mapping = {"status": status, **{k: str(v) for k, v in fields.items()}}
    await r.hset(job_key(job_id), mapping=mapping)
    await r.expire(job_key(job_id), JOB_TTL)

async def bump_attempts(r: redis.Redis, job_id: str) -> int:
    n = await r.hincrby(job_key(job_id), "attempts", 1)
    await r.expire(job_key(job_id), JOB_TTL)
    return int(n)

async def set_job_result(r: redis.Redis, job_id: str, result: Dict[str, Any]):
    await r.hset(job_key(job_id), mapping={
        "status": result.get("status", "DONE"),
        "result": json.dumps(result, ensure_ascii=False),
        "finished_at": str(int(time.time())),
    })
    await r.expire(job_key(job_id), JOB_TTL)

async def get_job(r: redis.Redis, job_id: str) -> Dict[str, Any]:
    data = await r.hgetall(job_key(job_id))
    return data or {}

async def send_to_dlq(r: redis.Redis, job_id: str, reason: str):
    await r.xadd(DLQ_STREAM, {"job_id": job_id, "reason": reason, "ts": str(int(time.time()))})
    await set_job_status(r, job_id, "DLQ", error=reason, finished_at=int(time.time()))

async def schedule_retry(r: redis.Redis, job_id: str, due_ts: float, reason: str):
    await r.zadd(DELAYED_ZSET, {job_id: float(due_ts)})
    await set_job_status(r, job_id, "RETRY_SCHEDULED", next_run_at=int(due_ts), error=reason)

async def pop_due_retry(r: redis.Redis, now_ts: float) -> str | None:
    item = await r.zpopmin(DELAYED_ZSET, 1)
    if not item:
        return None
    job_id, score = item[0]
    if float(score) <= float(now_ts):
        return job_id
    await r.zadd(DELAYED_ZSET, {job_id: float(score)})
    return None

async def requeue_job(r: redis.Redis, job_id: str) -> str:
    msg_id = await r.xadd(STREAM, {"job_id": job_id})
    await set_job_status(r, job_id, "QUEUED", msg_id=msg_id)
    return msg_id

async def dlq_recent(r: redis.Redis, count: int = 50) -> list[dict]:
    msgs = await r.xrevrange(DLQ_STREAM, max="+", min="-", count=count)
    out = []
    for msg_id, fields in msgs:
        out.append({"id": msg_id, **fields})
    return out
