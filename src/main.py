"""
main.py — FastAPI entry point for Pub-Sub Log Aggregator.

Endpoints:
    POST /publish          — Publish single or batch events
    GET  /events?topic=... — List unique processed events (optional topic filter)
    GET  /stats            — Aggregation counters + uptime
    GET  /health           — Liveness probe

Design:
    - lifespan context manager starts/stops the async consumer workers.
    - DedupStore and QueueManager are singletons instantiated at module level
      so they survive across requests within one process.
    - DB path is configurable via the DEDUP_DB_PATH environment variable,
      enabling easy overrides in tests and Docker bind-mounts.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .dedup_store import DedupStore
from .models import Event
from .queue_manager import QueueManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_DB_PATH = os.environ.get("DEDUP_DB_PATH", "/app/data/dedup.db")
_START_TIME = time.time()

dedup_store = DedupStore(db_path=_DB_PATH)
queue_manager = QueueManager(dedup_store=dedup_store, n_workers=2)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    queue_manager.start()
    logger.info("Aggregator service started. DB: %s", _DB_PATH)
    yield
    queue_manager.stop()
    logger.info("Aggregator service stopped.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Pub-Sub Log Aggregator",
    description="Idempotent consumer with persistent deduplication (UTS Sistem Terdistribusi)",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/publish", status_code=202)
async def publish(body: dict):
    """
    Accept a single event or a batch.

    Single event body:
        { "topic": "...", "event_id": "...", "timestamp": "...", "source": "...", "payload": {...} }

    Batch body:
        { "events": [ <event>, <event>, ... ] }
    """
    try:
        if "events" in body:
            # Batch path
            raw_events: list[dict] = body["events"]
            events = [Event(**e) for e in raw_events]
        else:
            # Single-event path
            events = [Event(**body)]
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    queued = await queue_manager.enqueue(events)
    return {"queued": queued, "message": "Events accepted and queued for processing"}


@app.get("/events")
async def get_events(topic: Optional[str] = Query(default=None, description="Filter by topic")):
    """
    Return all unique processed events.
    Optionally filter by ?topic=<name>.
    """
    # Give the async consumer a moment to flush the queue
    await queue_manager.drain(timeout=2.0)
    events = dedup_store.get_events(topic=topic)
    return {"events": events, "count": len(events), "topic_filter": topic}


@app.get("/stats")
async def get_stats():
    """
    Return aggregation counters and system metadata.

    Fields:
        received          — total events received (persisted)
        unique_processed  — events committed to dedup store (authoritative from DB)
        duplicate_dropped — duplicates caught (persisted)
        topics            — list of distinct topics in dedup store
        queue_size        — events currently waiting in the async queue
        uptime_seconds    — seconds since service start
    """
    return {
        "received": dedup_store.get_stat("received"),
        "unique_processed": dedup_store.count_unique(),   # DB-authoritative
        "duplicate_dropped": dedup_store.get_stat("duplicate_dropped"),
        "topics": dedup_store.get_topics(),
        "queue_size": queue_manager.queue_size,
        "uptime_seconds": round(time.time() - _START_TIME, 2),
    }


@app.get("/health")
async def health():
    """Liveness probe for Docker health-check."""
    return {"status": "ok", "uptime_seconds": round(time.time() - _START_TIME, 2)}


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=8080, reload=False)
