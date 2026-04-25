# uts-sister

Projek UTS Sistem Terdistribusi — Pub-Sub Log Aggregator dengan idempotent consumer dan deduplication pakai SQLite.

📹 **Demo YouTube**: https://youtu.be/ZCJkv1Li8bo

📄 **Laporan**: [Google Drive](https://drive.google.com/file/d/18NTurz9DJPDY7oJHdKc9KNrA2jhmq7ys/view?usp=sharing)

## Tentang Projek

Ini adalah log aggregator sederhana yang pakai model publish-subscribe. Publisher ngirim event ke aggregator lewat REST API, terus aggregator bakal cek apakah event tersebut sudah pernah diterima atau belum (deduplication). Kalau sudah pernah, event-nya di-drop biar ga duplikat.

Fitur yang diimplementasi:
- Publish event (single / batch) lewat `POST /publish`
- Lihat semua event yang sudah diproses di `GET /events`
- Cek statistik (jumlah received, unique, duplikat) di `GET /stats`
- Dedup store pakai SQLite (WAL mode), jadi data tetap ada walau container di-restart
- Publisher simulator yang sengaja ngirim event duplikat buat testing

## Cara Jalankan

### Pakai Docker

```bash
docker build -t uts-aggregator .

# jalankan biasa
docker run -p 8080:8080 uts-aggregator

# kalau mau data dedup-nya persist (ga hilang pas restart)
docker run -p 8080:8080 -v uts-data:/app/data uts-aggregator
```

### Pakai Docker Compose

Ini jalanin 2 service sekaligus (aggregator + publisher otomatis).

```bash
docker compose up --build

# buat liat log
docker compose logs -f

# stop
docker compose down
```

## Testing

```bash
pip install -r requirements.txt
pytest tests/ -v
```

Ada 10 test case yang cover validasi schema, deduplication, batch publish, dll.

## Endpoint

### POST /publish

Kirim satu event:
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

Kirim batch:
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

### GET /events

```bash
curl http://localhost:8080/events

# filter berdasarkan topic
curl "http://localhost:8080/events?topic=logs.app.error"
```

### GET /stats

```bash
curl http://localhost:8080/stats
```

Contoh response:
```json
{
  "received": 7500,
  "unique_processed": 5000,
  "duplicate_dropped": 2500,
  "topics": ["logs.app.error", "metrics.cpu"],
  "queue_size": 0,
  "uptime_seconds": 42.1
}
```

### GET /health

```bash
curl http://localhost:8080/health
```

## Contoh Tes Idempotency

Kirim event yang sama 3x, harusnya cuma 1 yang masuk:

```bash
for i in 1 2 3; do
  curl -s -X POST http://localhost:8080/publish \
    -H "Content-Type: application/json" \
    -d '{"topic":"demo","event_id":"SAME-ID-001","timestamp":"2024-01-01T00:00:00Z","source":"test","payload":{}}'
done

curl http://localhost:8080/stats
# unique_processed: 1, duplicate_dropped: 2
```

## Struktur Folder

```
uts-aggregator/
├── src/
│   ├── main.py            # app utama (FastAPI + endpoint)
│   ├── models.py          # model Event (Pydantic)
│   ├── dedup_store.py     # dedup pakai SQLite
│   ├── queue_manager.py   # queue + consumer
│   └── publisher.py       # publisher simulator
├── tests/
│   └── test_aggregator.py
├── Dockerfile
├── Dockerfile.publisher
├── docker-compose.yml
├── requirements.txt
├── report.md
└── README.md
```

## Batasan

- Cuma jalan di satu node (ga support clustering)
- Queue-nya in-memory (asyncio.Queue), jadi kalau crash sebelum diproses ya hilang. Tapi publisher bakal retry jadi masih aman
- SQLite oke buat single process, tapi kalau multi-process perlu ganti ke DB lain
- Belum ada auth (buat produksi harusnya pakai API key / JWT)
- Ordering cuma partial, ga dijamin urutan global-nya

## Referensi

Tanenbaum, A. S., & Van Steen, M. (2007). *Distributed systems: Principles and paradigms* (2nd ed.). Pearson Prentice Hall.
