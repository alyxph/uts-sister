"""
publisher.py — Standalone publisher script.

Simulates at-least-once delivery by intentionally re-sending a configurable
fraction of events (duplicates).  Used for:
    1. Manual load/smoke testing.
    2. Docker Compose 'publisher' service that targets the aggregator container.

Environment variables:
    AGGREGATOR_URL  — base URL of aggregator (default: http://localhost:8080)
    TOTAL_EVENTS    — how many unique events to generate (default: 6000)
    DUPLICATE_RATE  — fraction of events to send as duplicates (default: 0.25)
    BATCH_SIZE      — events per POST /publish call (default: 100)
    TOPIC_COUNT     — number of distinct topics (default: 5)
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AGGREGATOR_URL: str = os.environ.get("AGGREGATOR_URL", "http://localhost:8080")
TOTAL_EVENTS: int = int(os.environ.get("TOTAL_EVENTS", "6000"))
DUPLICATE_RATE: float = float(os.environ.get("DUPLICATE_RATE", "0.25"))
BATCH_SIZE: int = int(os.environ.get("BATCH_SIZE", "100"))
TOPIC_COUNT: int = int(os.environ.get("TOPIC_COUNT", "5"))


def make_event(topic: str, event_id: str) -> dict:
    return {
        "topic": topic,
        "event_id": event_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "publisher-sim",
        "payload": {"value": random.randint(1, 9999), "seq": event_id},
    }


async def run() -> None:
    topics = [f"topic.{chr(65 + i)}" for i in range(TOPIC_COUNT)]
    unique_events = [
        make_event(random.choice(topics), str(uuid.uuid4())) for _ in range(TOTAL_EVENTS)
    ]

    # Build send list: unique + synthetic duplicates
    send_list = list(unique_events)
    n_dups = int(TOTAL_EVENTS * DUPLICATE_RATE)
    dup_events = [dict(e, timestamp=datetime.now(timezone.utc).isoformat())
                  for e in random.choices(unique_events, k=n_dups)]
    send_list.extend(dup_events)
    random.shuffle(send_list)

    total_to_send = len(send_list)
    logger.info(
        "Publisher: %d unique + %d duplicates = %d total events across %d topics",
        TOTAL_EVENTS,
        n_dups,
        total_to_send,
        TOPIC_COUNT,
    )

    sent = 0
    errors = 0
    t0 = time.perf_counter()

    async with httpx.AsyncClient(base_url=AGGREGATOR_URL, timeout=30) as client:
        # Wait for aggregator readiness
        for _ in range(20):
            try:
                r = await client.get("/health")
                if r.status_code == 200:
                    logger.info("Aggregator is ready.")
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
        else:
            logger.error("Aggregator not reachable at %s — aborting.", AGGREGATOR_URL)
            return

        for i in range(0, total_to_send, BATCH_SIZE):
            batch = send_list[i : i + BATCH_SIZE]
            try:
                resp = await client.post("/publish", json={"events": batch})
                resp.raise_for_status()
                sent += len(batch)
            except Exception as exc:
                logger.error("Batch %d failed: %s", i // BATCH_SIZE, exc)
                errors += 1

    elapsed = time.perf_counter() - t0
    rate = sent / elapsed if elapsed > 0 else 0
    logger.info(
        "Done. Sent %d events in %.2fs (%.0f events/s). Errors: %d",
        sent,
        elapsed,
        rate,
        errors,
    )

    # Print final stats
    try:
        import httpx as _httpx
        resp = _httpx.get(f"{AGGREGATOR_URL}/stats", timeout=5)
        logger.info("Aggregator stats: %s", resp.json())
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(run())
