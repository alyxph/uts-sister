"""
tests/test_aggregator.py — Unit & integration tests for Pub-Sub Log Aggregator.

Coverage:
    1.  Schema validation — valid event passes.
    2.  Schema validation — missing required field raises.
    3.  Schema validation — malformed ISO 8601 timestamp raises.
    4.  Deduplication — duplicate event is dropped (processed only once).
    5.  Deduplication persistence — simulated restart via fresh DedupStore instance.
    6.  POST /publish single event → 202 accepted.
    7.  POST /publish batch events → queued count matches.
    8.  GET /events returns only unique events.
    9.  GET /stats returns consistent counters.
    10. Stress test — 5 000 events with 25 % duplicates processed in < 30 s.

Run with:
    pytest tests/ -v
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _eid() -> str:
    """Return a random UUID string as event_id."""
    return str(uuid.uuid4())


def _event(
    topic: str = "test.topic",
    event_id: str | None = None,
    source: str = "unit-test",
    payload: dict | None = None,
) -> dict:
    return {
        "topic": topic,
        "event_id": event_id or _eid(),
        "timestamp": _ts(),
        "source": source,
        "payload": payload or {},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def tmp_db(tmp_path):
    """Provide a temporary SQLite path isolated per test."""
    return str(tmp_path / "test_dedup.db")


@pytest.fixture(scope="function")
def dedup(tmp_db):
    """Fresh DedupStore backed by a temp DB."""
    from src.dedup_store import DedupStore
    return DedupStore(db_path=tmp_db)


@pytest.fixture(scope="function")
def app_client(tmp_db, monkeypatch):
    """
    TestClient for the FastAPI app with an isolated temp DB.
    Uses context manager to trigger lifespan (starts consumer workers).
    Injects fresh DedupStore and QueueManager singletons per test.
    """
    import src.dedup_store as ds_mod
    import src.main as main_mod
    import src.queue_manager as qm_mod

    # Fresh isolated instances
    fresh_store = ds_mod.DedupStore(db_path=tmp_db)
    fresh_qm = qm_mod.QueueManager(dedup_store=fresh_store, n_workers=2)

    # Inject before lifespan runs
    monkeypatch.setattr(main_mod, "dedup_store", fresh_store)
    monkeypatch.setattr(main_mod, "queue_manager", fresh_qm)

    # Context manager triggers lifespan → fresh_qm.start() is called
    with TestClient(main_mod.app) as client:
        yield client


# ---------------------------------------------------------------------------
# 1. Schema validation — valid event
# ---------------------------------------------------------------------------

def test_valid_event_schema():
    from src.models import Event

    e = Event(
        topic="logs.app",
        event_id="abc-123",
        timestamp="2024-01-01T00:00:00+00:00",
        source="svc-a",
        payload={"level": "INFO"},
    )
    assert e.topic == "logs.app"
    assert e.event_id == "abc-123"


# ---------------------------------------------------------------------------
# 2. Schema validation — missing required field
# ---------------------------------------------------------------------------

def test_missing_field_raises():
    from pydantic import ValidationError
    from src.models import Event

    with pytest.raises(ValidationError):
        Event(
            # topic intentionally omitted
            event_id="x",
            timestamp="2024-01-01T00:00:00Z",
            source="svc",
        )


# ---------------------------------------------------------------------------
# 3. Schema validation — invalid timestamp
# ---------------------------------------------------------------------------

def test_invalid_timestamp_raises():
    from pydantic import ValidationError
    from src.models import Event

    with pytest.raises(ValidationError):
        Event(
            topic="t",
            event_id="e",
            timestamp="not-a-date",
            source="s",
        )


# ---------------------------------------------------------------------------
# 4. Deduplication — duplicate dropped
# ---------------------------------------------------------------------------

def test_dedup_drops_duplicate(dedup):
    from src.models import Event

    evt = Event(**_event(event_id="dup-001"))

    first = dedup.mark_processed(evt)
    second = dedup.mark_processed(evt)

    assert first is True,  "First insertion should succeed"
    assert second is False, "Second insertion should be detected as duplicate"
    assert dedup.count_unique() == 1


# ---------------------------------------------------------------------------
# 5. Deduplication persistence — simulated restart
# ---------------------------------------------------------------------------

def test_dedup_persists_across_restart(tmp_db):
    """
    Simulates a container restart by discarding the first DedupStore instance
    and creating a new one pointing at the same SQLite file.
    """
    from src.dedup_store import DedupStore
    from src.models import Event

    evt = Event(**_event(event_id="persist-evt-999"))

    store1 = DedupStore(db_path=tmp_db)
    assert store1.mark_processed(evt) is True

    # "Restart" — new store instance, same file
    store2 = DedupStore(db_path=tmp_db)
    assert store2.mark_processed(evt) is False, \
        "After restart, duplicate must still be rejected"
    assert store2.is_duplicate("test.topic", "persist-evt-999")


# ---------------------------------------------------------------------------
# 6. POST /publish — single event accepted
# ---------------------------------------------------------------------------

def test_publish_single_event(app_client):
    payload = _event()
    resp = app_client.post("/publish", json=payload)
    assert resp.status_code == 202
    data = resp.json()
    assert data["queued"] == 1


# ---------------------------------------------------------------------------
# 7. POST /publish — batch accepted
# ---------------------------------------------------------------------------

def test_publish_batch(app_client):
    batch = {"events": [_event() for _ in range(10)]}
    resp = app_client.post("/publish", json=batch)
    assert resp.status_code == 202
    assert resp.json()["queued"] == 10


# ---------------------------------------------------------------------------
# 8. GET /events — returns unique events only
# ---------------------------------------------------------------------------

def test_get_events_unique_only(app_client):
    shared_id = _eid()

    # Send the same event 3 times
    for _ in range(3):
        app_client.post("/publish", json=_event(event_id=shared_id))

    # Send 4 unique events
    for _ in range(4):
        app_client.post("/publish", json=_event())

    # Poll until queue drains (max 5s)
    deadline = time.time() + 5
    while time.time() < deadline:
        stats = app_client.get("/stats").json()
        if stats["queue_size"] == 0:
            break
        time.sleep(0.1)

    resp = app_client.get("/events")
    assert resp.status_code == 200
    events = resp.json()["events"]
    ids = [e["event_id"] for e in events]
    assert ids.count(shared_id) == 1, "Duplicate event_id must appear only once"
    assert len(set(ids)) == len(ids), "All returned event_ids must be unique"


# ---------------------------------------------------------------------------
# 9. GET /stats — consistent counters
# ---------------------------------------------------------------------------

def test_stats_consistent(app_client):
    n_unique = 5
    n_dups = 3
    base_id = _eid()

    # Send unique events
    for _ in range(n_unique):
        app_client.post("/publish", json=_event())

    # Send duplicates
    for _ in range(n_dups):
        app_client.post("/publish", json=_event(event_id=base_id))

    # Poll until queue drains (max 5s)
    deadline = time.time() + 5
    while time.time() < deadline:
        stats_check = app_client.get("/stats").json()
        if stats_check["queue_size"] == 0:
            break
        time.sleep(0.1)

    resp = app_client.get("/stats")
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["received"] >= n_unique + n_dups
    assert stats["duplicate_dropped"] >= n_dups - 1  # first send is unique
    assert "uptime_seconds" in stats
    assert isinstance(stats["topics"], list)


# ---------------------------------------------------------------------------
# 10. Stress test — 5 000 events, ≥25 % duplicates, < 30 s
# ---------------------------------------------------------------------------

def test_stress_5000_events(tmp_db):
    """
    Sends 5 000 unique events plus 1 250 duplicates (25 %) through the
    DedupStore directly (bypasses HTTP overhead for speed).
    Asserts correctness and timing.
    """
    from src.dedup_store import DedupStore
    from src.models import Event

    store = DedupStore(db_path=tmp_db)

    total_unique = 5000
    dup_rate = 0.25
    events = [Event(**_event(topic="stress.topic", event_id=str(i))) for i in range(total_unique)]

    import random
    dup_events = random.choices(events, k=int(total_unique * dup_rate))
    all_events = events + dup_events
    random.shuffle(all_events)

    t0 = time.perf_counter()
    new_count = 0
    dup_count = 0
    for evt in all_events:
        if store.mark_processed(evt):
            new_count += 1
        else:
            dup_count += 1
    elapsed = time.perf_counter() - t0

    assert new_count == total_unique, f"Expected {total_unique} unique, got {new_count}"
    assert dup_count == int(total_unique * dup_rate), \
        f"Expected {int(total_unique * dup_rate)} duplicates, got {dup_count}"
    assert elapsed < 30, f"Processing took too long: {elapsed:.2f}s"
    assert store.count_unique() == total_unique
