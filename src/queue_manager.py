"""
queue_manager.py — Asynchronous internal queue and idempotent consumer.

Architecture:
    Publisher → asyncio.Queue → Consumer → DedupStore → Processed events

The consumer loop is a single asyncio coroutine.  For higher throughput,
multiple consumer workers can be spawned (see `n_workers` parameter).
Deduplication is delegated entirely to DedupStore.mark_processed(), which
provides atomic INSERT OR IGNORE semantics at the SQLite level.

At-least-once delivery is simulated when publishers send duplicate event_ids.
The consumer handles this transparently: each call to mark_processed() either
commits the event (new) or silently drops it (duplicate).
"""

from __future__ import annotations

import asyncio
import logging

from .dedup_store import DedupStore
from .models import Event

logger = logging.getLogger(__name__)


class QueueManager:
    """
    Manages the internal asyncio.Queue and one or more consumer workers.

    Attributes:
        stats: In-memory counters.  These reset on restart; the authoritative
               source of unique_processed truth is DedupStore.count_unique().
    """

    def __init__(self, dedup_store: DedupStore, n_workers: int = 2) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._dedup = dedup_store
        self._n_workers = n_workers
        self._tasks: list[asyncio.Task] = []

        # Stats are now persisted in DedupStore

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn consumer worker coroutines. Must be called inside an event loop."""
        for i in range(self._n_workers):
            task = asyncio.create_task(self._consume(worker_id=i))
            self._tasks.append(task)
            logger.info("Consumer worker #%d started", i)

    def stop(self) -> None:
        """Cancel all consumer tasks gracefully."""
        for task in self._tasks:
            task.cancel()
        logger.info("All consumer workers stopped")

    # ------------------------------------------------------------------
    # Publisher interface
    # ------------------------------------------------------------------

    async def enqueue(self, events: list[Event]) -> int:
        """Put events onto the queue and return the number enqueued."""
        self._dedup.increment_stat("received", len(events))
        for event in events:
            await self._queue.put(event)
        return len(events)

    async def drain(self, timeout: float = 5.0) -> None:
        """Block until the queue is empty or timeout elapses."""
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("drain() timed out; %d items still in queue", self._queue.qsize())

    # ------------------------------------------------------------------
    # Consumer
    # ------------------------------------------------------------------

    async def _consume(self, worker_id: int) -> None:
        logger.info("Worker #%d ready", worker_id)
        while True:
            event: Event = await self._queue.get()
            try:
                is_new = self._dedup.mark_processed(event)
                if is_new:
                    logger.info(
                        "[W%d] PROCESSED  topic=%-20s event_id=%s",
                        worker_id,
                        event.topic,
                        event.event_id,
                    )
                else:
                    self._dedup.increment_stat("duplicate_dropped", 1)
                    logger.warning(
                        "[W%d] DUPLICATE  topic=%-20s event_id=%s — DROPPED",
                        worker_id,
                        event.topic,
                        event.event_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("[W%d] Error processing event %s: %s", worker_id, event.event_id, exc)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()
