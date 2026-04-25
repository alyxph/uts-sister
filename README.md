# Pub-Sub Log Aggregator

> UTS Sistem Terdistribusi dan Parallel — Idempotent Consumer + Persistent Deduplication

---

## Fitur Utama

- **POST /publish** — Terima single event atau batch
- **GET /events** — Daftar event unik yang telah diproses
- **GET /stats** — Statistik real-time (received, unique, duplicates, uptime)
- **Idempotent consumer** — Event yang sama tidak diproses lebih dari sekali
- **Persistent dedup store** — SQLite WAL; tahan restart container
- **At-least-once simulation** — Publisher mengirim duplikat secara sengaja

---

## Cara Build & Run

### Minimal (Docker saja)

```bash
# Build image
docker build -t uts-aggregator .

# Run container (dedup store ephemeral, reset saat restart)
docker run -p 8080:8080 uts-aggregator

# Run dengan persistent volume (dedup bertahan setelah restart)
docker run -p 8080:8080 -v uts-data:/app/data uts-aggregator
```

### Dengan Docker Compose (Bonus — dua service terpisah)

```bash
# Build & jalankan aggregator + publisher secara bersamaan
docker compose up --build

# Lihat log real-time
docker compose logs -f

# Hentikan semua service
docker compose down
```

---

## Menjalankan Unit Tests

```bash
# Instal dependencies (Python 3.11+)
pip install -r requirements.txt

# Jalankan semua tests
pytest tests/ -v

# Output yang diharapkan: 10 tests passed
```

---

## Endpoint API

### `POST /publish`

Terima single event atau batch.

**Single event:**
```bash
curl -X POST http://localhost:8080/publish \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "logs.app.error",
    "event_id": "550e8400-e29b-41d4-a716-446655440000",
    "timestamp": "2024-01-01T10:00:00+00:00",
    "source": "service-A",
    "payload": {"level": "ERROR", "message": "NullPointerException"}
  }'
```

**Batch:**
```bash
curl -X POST http://localhost:8080/publish \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"topic": "logs.app", "event_id": "evt-001", "timestamp": "2024-01-01T10:00:00Z", "source": "svc-a", "payload": {}},
      {"topic": "logs.app", "event_id": "evt-002", "timestamp": "2024-01-01T10:00:01Z", "source": "svc-a", "payload": {}}
    ]
  }'
```

### `GET /events?topic=<topic>`

```bash
# Semua event unik
curl http://localhost:8080/events

# Filter by topic
curl "http://localhost:8080/events?topic=logs.app.error"
```

### `GET /stats`

```bash
curl http://localhost:8080/stats
# Response:
# {
#   "received": 7500,
#   "unique_processed": 5000,
#   "duplicate_dropped": 2500,
#   "topics": ["logs.app.error", "metrics.cpu"],
#   "queue_size": 0,
#   "uptime_seconds": 42.1
# }
```

### `GET /health`

```bash
curl http://localhost:8080/health
```

---

## Demonstrasi Idempotency & Deduplication

```bash
# Kirim event yang sama 3 kali
for i in 1 2 3; do
  curl -s -X POST http://localhost:8080/publish \
    -H "Content-Type: application/json" \
    -d '{"topic":"demo","event_id":"SAME-ID-001","timestamp":"2024-01-01T00:00:00Z","source":"test","payload":{}}'
done

# Cek: hanya 1 event tersimpan, 2 didrop
curl http://localhost:8080/stats
# "unique_processed": 1, "duplicate_dropped": 2
```

---

## Struktur Direktori

```
uts-aggregator/
├── src/
│   ├── __init__.py
│   ├── main.py          # FastAPI app, endpoints, lifespan
│   ├── models.py        # Pydantic Event model + validation
│   ├── dedup_store.py   # SQLite-backed deduplication store
│   ├── queue_manager.py # asyncio.Queue + idempotent consumer
│   └── publisher.py     # Standalone publisher simulator
├── tests/
│   └── test_aggregator.py  # 10 unit/integration tests
├── requirements.txt
├── Dockerfile              # Aggregator image (non-root, python:3.11-slim)
├── Dockerfile.publisher    # Publisher image (Docker Compose bonus)
├── docker-compose.yml      # Bonus: dua service terpisah
├── report.md               # Laporan teori T1–T8 + sitasi APA
└── README.md               # File ini
```

---

## Asumsi & Batasan

1. **Single-node** — Sistem berjalan dalam satu container/proses. Tidak mendukung clustering.
2. **In-memory queue** — asyncio.Queue tidak persistent. Event yang ada di queue saat crash akan hilang (acceptable untuk at-least-once karena publisher akan retry).
3. **SQLite concurrency** — Cocok untuk satu proses dengan multiple thread/coroutine. Tidak cocok untuk multi-process tanpa connection pooling.
4. **No authentication** — Endpoint publik tanpa auth. Untuk produksi, tambahkan API key atau JWT.
5. **Ordering** — Partial ordering berdasarkan waktu pemrosesan. Total ordering global tidak dijamin.

---

## Video Demo

Link YouTube: _[Tambahkan link setelah upload]_

Durasi: 5–8 menit  
Konten:
1. Build image dan run container
2. Publish event duplikat — tampilkan idempotency di log
3. GET /events dan GET /stats sebelum/sesudah duplikat
4. Restart container — dedup store persisten mencegah reprocessing
5. Ringkasan arsitektur (30–60 detik)

---

## Referensi

Tanenbaum, A. S., & Van Steen, M. (2007). *Distributed systems: Principles and paradigms* (2nd ed.). Pearson Prentice Hall.
# uts-sister
