# Laporan UTS: Pub-Sub Log Aggregator dengan Idempotent Consumer dan Deduplication

**Mata Kuliah:** Sistem Terdistribusi dan Parallel  
**Nama:** [Nama Mahasiswa]  
**NIM:** [NIM]  
**Tanggal:** 2025

---

## Daftar Isi

1. [Ringkasan Sistem & Arsitektur](#1-ringkasan-sistem--arsitektur)
2. [Bagian Teori (T1–T8)](#2-bagian-teori-t1t8)
3. [Keputusan Desain Implementasi](#3-keputusan-desain-implementasi)
4. [Analisis Performa & Metrik](#4-analisis-performa--metrik)
5. [Keterkaitan ke Bab 1–7](#5-keterkaitan-ke-bab-17)
6. [Referensi](#6-referensi)

---

## 1. Ringkasan Sistem & Arsitektur

### 1.1 Gambaran Umum

Sistem ini adalah sebuah **Pub-Sub Log Aggregator** yang berjalan sepenuhnya lokal di dalam container Docker. Sistem menerima event/log dari publisher melalui HTTP, memprosesnya melalui internal queue, dan menjamin bahwa setiap event hanya diproses satu kali melalui mekanisme **idempotent consumer** dan **persistent deduplication store**.

### 1.2 Diagram Arsitektur

```
┌─────────────────────────────────────────────────────────────────┐
│                         Docker Container                        │
│                                                                 │
│   ┌──────────┐   POST /publish   ┌──────────────────────────┐   │
│   │Publisher │──────────────────▶│     FastAPI (port 8080)  │   │
│   │(external │                   │                          │   │
│   │ or sim)  │                   │  ┌─────────────────────┐ │   │
│   └──────────┘                   │  │  Schema Validation  │ │   │
│                                  │  │  (Pydantic v2)      │ │   │
│                                  │  └────────┬────────────┘ │   │
│   ┌──────────┐   GET /events     │           │ enqueue()    │   │
│   │Consumer  │◀──────────────────│  ┌────────▼────────────┐ │   │
│   │(external)│   GET /stats      │  │  asyncio.Queue      │ │   │
│   └──────────┘                   │  │  (in-memory buffer) │ │   │
│                                  │  └────────┬────────────┘ │   │
│                                  │           │ consume()    │   │
│                                  │  ┌────────▼────────────┐ │   │
│                                  │  │  Idempotent Consumer│ │   │
│                                  │  │  (2 async workers)  │ │   │
│                                  │  └────────┬────────────┘ │   │
│                                  │           │mark_processed│   │
│                                  │  ┌────────▼────────────┐ │   │
│                                  │  │   DedupStore        │ │   │
│                                  │  │   (SQLite WAL)      │ │   │
│                                  │  │  PK: (topic,        │ │   │
│                                  │  │       event_id)     │ │   │
│                                  │  └─────────────────────┘ │   │
│                                  └──────────────────────────┘   │
│                                                                  │
│   /app/data/dedup.db  ◀── Persistent Volume (survives restart)  │
└─────────────────────────────────────────────────────────────────┘
```

### 1.3 Komponen Utama

| Komponen | Teknologi | Peran |
|---|---|---|
| HTTP API | FastAPI + Uvicorn | Endpoint /publish, /events, /stats |
| Internal Queue | `asyncio.Queue` | Buffer antara publisher dan consumer |
| Consumer Worker | `asyncio.Task` (×2) | Memproses event secara concurrent |
| Dedup Store | SQLite (WAL mode) | Persistent idempotency tracker |
| Schema Validation | Pydantic v2 | Validasi format event |

---

## 2. Bagian Teori (T1–T8)

### T1 — Karakteristik Sistem Terdistribusi & Trade-off Pub-Sub Log Aggregator
*(Bab 1 — Tanenbaum & Van Steen, 2007)*

Sistem terdistribusi memiliki beberapa karakteristik utama yang saling berkaitan, namun juga saling berkonflik satu sama lain. Tanenbaum & Van Steen (2007) mendefinisikan sistem terdistribusi sebagai kumpulan komputer independen yang tampak bagi pengguna sebagai satu sistem koheren.

**Karakteristik utama:**

1. **Resource sharing** — Komponen-komponen berbagi sumber daya (storage, CPU) namun tetap beroperasi secara mandiri. Dalam Pub-Sub aggregator, publisher dan subscriber berbagi topik sebagai sumber daya logis.

2. **Openness** — Sistem harus dapat diperluas tanpa mengubah komponen yang ada. HTTP REST API pada aggregator memungkinkan publisher dan subscriber baru ditambahkan tanpa modifikasi kode inti.

3. **Scalability** — Kemampuan menangani beban yang meningkat. Pub-Sub memfasilitasi horizontal scaling karena publisher tidak perlu mengetahui jumlah atau lokasi subscriber.

4. **Concurrency** — Beberapa proses beroperasi simultan. asyncio.Queue memediasi concurrency antara HTTP handler (producer) dan consumer worker tanpa blocking.

5. **Fault tolerance** — Sistem harus tetap berjalan meski ada kegagalan parsial. Dedup store yang persistent memungkinkan idempotency terjaga bahkan setelah crash dan restart.

**Trade-off utama pada Pub-Sub Log Aggregator:**

Salah satu trade-off paling fundamental adalah antara **consistency** dan **availability** (teorema CAP). Pada aggregator ini, pilihan at-least-once delivery berarti kita mengutamakan availability (event tidak akan hilang) dengan mengorbankan exactly-once consistency (duplikat mungkin terjadi di sisi transport). Konsistensi kemudian dipulihkan oleh lapisan deduplication, bukan oleh lapisan transport.

Trade-off kedua adalah antara **throughput** dan **latency**. Penggunaan batch dalam `POST /publish` meningkatkan throughput, namun menambah latency per-event karena harus menunggu batch penuh. Penggunaan `asyncio.Queue` juga menambah latency satu hop (queue-consumer) namun memungkinkan decoupling temporal antara publisher dan consumer.

*(Tanenbaum & Van Steen, 2007, Bab 1)*

---

### T2 — Client-Server vs Publish-Subscribe untuk Log Aggregator
*(Bab 2 — Tanenbaum & Van Steen, 2007)*

**Arsitektur Client-Server:**
Dalam model client-server klasik, publisher (client) terhubung langsung ke aggregator (server) dan mengirim log secara sinkron. Subscriber juga langsung menghubungi server untuk mengambil data (pull model). Keunggulannya adalah kesederhanaan implementasi dan kontrol penuh atas request-response lifecycle. Kelemahannya: publisher dan subscriber tightly coupled — publisher harus mengetahui alamat server, dan server harus aktif agar log tidak hilang. Jika ada 1.000 subscriber yang melakukan polling setiap detik, server dapat kewalahan.

**Arsitektur Publish-Subscribe:**
Model Pub-Sub memperkenalkan lapisan intermediasi (*broker* atau *event bus*) yang memisahkan publisher dari subscriber secara spasial, temporal, dan sinkronisasi. Publisher hanya mengetahui topik, bukan identitas subscriber. Subscriber mendaftar ke topik yang relevan. Keunggulan utama: **decoupling** — publisher dapat berjalan tanpa mengetahui siapa atau berapa banyak subscriber; sistem lebih mudah di-scale; penambahan subscriber baru tidak memerlukan perubahan pada publisher.

**Kapan memilih Pub-Sub?**

Pub-Sub adalah pilihan tepat untuk log aggregator karena:
1. **Multiple consumers** — Log perlu dikonsumsi oleh berbagai sistem (monitoring, alerting, analytics) secara independen.
2. **Temporal decoupling** — Publisher tidak boleh terhambat oleh kecepatan processing subscriber.
3. **Bursty traffic** — Log sering datang dalam burst besar; queue menjadi buffer elastis.
4. **Fault isolation** — Kegagalan satu consumer tidak mempengaruhi publisher atau consumer lain.

Dalam implementasi ini, meskipun berjalan dalam satu proses, `asyncio.Queue` merealisasikan prinsip Pub-Sub: HTTP handler berperan sebagai publisher yang memasukkan event ke queue, sementara consumer worker berperan sebagai subscriber yang memproses event secara asinkron.

*(Tanenbaum & Van Steen, 2007, Bab 2)*

---

### T3 — At-Least-Once vs Exactly-Once & Pentingnya Idempotent Consumer
*(Bab 3 — Tanenbaum & Van Steen, 2007)*

**At-least-once delivery:**
Jaminan ini memastikan setiap event pasti sampai ke consumer minimal satu kali. Implementasinya sederhana: publisher mengirim ulang (*retry*) jika tidak menerima acknowledgment. Akibatnya, dalam kondisi jaringan tidak stabil atau timeout, event yang sama bisa dikirim lebih dari sekali — menciptakan duplikasi. Inilah skenario yang disimulasikan oleh `src/publisher.py` yang sengaja mengirim ulang 25% event.

**Exactly-once delivery:**
Jaminan ini memastikan setiap event diproses tepat sekali — tidak kurang, tidak lebih. Implementasinya jauh lebih kompleks: membutuhkan koordinasi dua fase (*two-phase commit*), idempotent keys, atau transactional messaging (seperti yang dimiliki Apache Kafka dengan `enable.idempotence=true`). Overhead koordinasi ini berdampak signifikan pada latency dan throughput.

**Mengapa idempotent consumer krusial?**

Karena *exactly-once* sulit dicapai di lapisan transport (terutama dengan sistem terdistribusi yang rentan crash), pendekatan pragmatis yang umum diadopsi adalah: **gunakan at-least-once delivery di lapisan transport, dan terapkan idempotency di lapisan consumer** (Tanenbaum & Van Steen, 2007, Bab 3). 

Sebuah consumer disebut **idempotent** jika memproses event yang sama dua kali menghasilkan state akhir yang identik dengan memproses event tersebut sekali. Dalam implementasi ini, idempotency dicapai melalui `INSERT OR IGNORE` pada SQLite dengan primary key `(topic, event_id)`: mencoba menyimpan event yang sama dua kali hanya menghasilkan satu baris di database, bukan error atau duplikasi. Dengan demikian, kombinasi at-least-once + idempotent consumer secara efektif memberikan semantik *effectively-once* dari perspektif state akhir.

*(Tanenbaum & Van Steen, 2007, Bab 3)*

---

### T4 — Skema Penamaan Topic & Event ID
*(Bab 4 — Tanenbaum & Van Steen, 2007)*

Tanenbaum & Van Steen (2007, Bab 4) mendefinisikan penamaan sebagai mekanisme fundamental dalam sistem terdistribusi untuk mengidentifikasi dan menemukan sumber daya. Kualitas skema penamaan langsung mempengaruhi efisiensi deduplication.

**Skema Penamaan Topic:**

Format yang diadopsi: `<domain>.<subdomain>.<qualifier>` (contoh: `logs.app.error`, `metrics.cpu.usage`).

Keunggulan skema hierarkis ini:
- **Namespacing** — mencegah collision antar tim/service yang berbeda.
- **Filterability** — subscriber dapat berlangganan `logs.app.*` untuk semua log aplikasi.
- **Human-readable** — memudahkan debugging dan monitoring.
- **Collision resistance** — kombinasi domain + subdomain secara statistik sangat kecil kemungkinannya bertabrakan.

**Skema Event ID:**

Format: UUID v4 (contoh: `550e8400-e29b-41d4-a716-446655440000`).

UUID v4 adalah string 128-bit yang dibangkitkan secara acak dengan probabilitas collision yang dapat diabaikan secara praktis (sekitar 1 dalam 2^61 untuk 103 triliun UUID). Ini jauh lebih baik daripada timestamp saja (yang dapat bertabrakan pada sistem dengan resolusi rendah atau sistem terdistribusi tanpa sinkronisasi clock yang baik).

**Dampak terhadap Deduplication:**

Primary key `(topic, event_id)` pada dedup store menghasilkan compound key yang:
1. Unik per event per topik.
2. Tidak bergantung pada urutan penerimaan.
3. Deterministik — publisher yang sama mengirim event yang sama menghasilkan key yang sama, sehingga deduplication bekerja terlepas dari berapa kali event tersebut di-retry.

Tanpa skema penamaan yang baik, deduplication menjadi tidak mungkin atau tidak akurat: timestamp saja tidak cukup karena dua event berbeda bisa memiliki timestamp sama (clock resolution rendah), dan source saja tidak cukup karena satu source dapat mengirim ribuan event.

*(Tanenbaum & Van Steen, 2007, Bab 4)*

---

### T5 — Ordering: Kapan Total Ordering Tidak Diperlukan?
*(Bab 5 — Tanenbaum & Van Steen, 2007)*

Tanenbaum & Van Steen (2007, Bab 5) membahas konsep Lamport timestamps dan vector clocks sebagai mekanisme untuk menetapkan urutan kausal (*causal ordering*) pada event dalam sistem terdistribusi. Total ordering — urutan global yang konsisten di seluruh node — adalah properti yang mahal untuk dicapai karena membutuhkan koordinasi global atau penggunaan atomic clock yang presisi.

**Kapan total ordering tidak diperlukan?**

Untuk log aggregator, total ordering biasanya **tidak diperlukan** karena:

1. **Log bersifat commutative dalam analisis** — Sebuah sistem monitoring yang menghitung jumlah error per menit tidak peduli apakah `error-A` atau `error-B` diproses lebih dulu; hasilnya sama.

2. **Causal ordering sudah cukup** — Jika kita hanya perlu tahu bahwa "request masuk sebelum response dikirim", causal ordering via Lamport clock sudah memadai tanpa total ordering global.

3. **Cost vs benefit** — Menjamin total ordering membutuhkan protokol konsensus (seperti Paxos atau Raft) yang mengorbankan throughput dan menambah latency secara signifikan. Untuk log aggregator dengan throughput ribuan event/detik, overhead ini tidak sepadan.

**Pendekatan praktis yang diusulkan:**

Kombinasi **ISO 8601 timestamp + monotonic sequence counter per source**:
```json
{
  "timestamp": "2024-01-01T10:00:00.000Z",
  "source": "service-A",
  "seq": 4201
}
```

Timestamp memungkinkan approximate ordering antar source. Sequence counter per source menjamin ordering dalam satu source meski clock skew terjadi. Jika dua event memiliki timestamp identik dari source berbeda, ordering tidak dijamin — namun untuk use case aggregator (statistik, monitoring), ini dapat diterima.

**Batasan:**
- Clock skew antar server (biasanya 1–100ms) dapat menyebabkan out-of-order delivery.
- NTP tidak menjamin monotonicity di seluruh node.
- Untuk use case yang membutuhkan total ordering (seperti audit log keuangan), diperlukan solusi seperti Kafka dengan single partition atau database dengan global sequence.

Dalam implementasi ini, **partial ordering** (berdasarkan `processed_at` di SQLite) digunakan untuk `GET /events`, yang sudah cukup untuk kebutuhan monitoring dan debugging.

*(Tanenbaum & Van Steen, 2007, Bab 5)*

---

### T6 — Failure Modes & Strategi Mitigasi
*(Bab 6 — Tanenbaum & Van Steen, 2007)*

Tanenbaum & Van Steen (2007, Bab 6) mengklasifikasikan kegagalan dalam sistem terdistribusi ke dalam beberapa kategori: crash failures, omission failures, timing failures, dan Byzantine failures. Berikut adalah failure modes yang relevan untuk Pub-Sub aggregator beserta strategi mitigasinya:

**1. Duplikasi event (At-least-once retries):**
- **Penyebab:** Publisher mengirim ulang event karena timeout atau network partition, padahal event sebelumnya sudah diterima.
- **Mitigasi:** Idempotent consumer dengan dedup store. `INSERT OR IGNORE` pada SQLite menjamin idempotency atomik. Logging eksplisit (`DUPLICATE detected`) memungkinkan monitoring tingkat duplikasi.

**2. Out-of-order delivery:**
- **Penyebab:** Event dari sumber yang sama bisa tiba dalam urutan berbeda akibat perbedaan jalur jaringan atau retry yang tidak terurut.
- **Mitigasi:** Aggregator tidak memaksa total ordering (lihat T5). Event diproses berdasarkan urutan kedatangan dan disimpan dengan `processed_at` timestamp. Untuk analisis yang membutuhkan ordering, consumer dapat men-sort berdasarkan `event.timestamp` setelah mengambil data dari `GET /events`.

**3. Crash dan restart container:**
- **Penyebab:** OOM killer, deployment baru, atau hardware failure.
- **Mitigasi:** DedupStore menggunakan SQLite dengan journaling (WAL mode) yang memberikan durabilitas ACID. Event yang sudah diproses sebelum crash akan tetap tercatat setelah restart, mencegah reprocessing. Volume Docker (`/app/data`) di-mount secara persistent.

**4. Queue overflow (backpressure):**
- **Penyebab:** Publisher menghasilkan event lebih cepat dari kemampuan consumer memproses.
- **Mitigasi:** `asyncio.Queue` bersifat unbounded secara default (berdasarkan RAM), memberikan buffer yang fleksibel. Endpoint `GET /stats` mengekspos `queue_size` untuk monitoring. Untuk produksi, backpressure eksplisit dapat diterapkan dengan `asyncio.Queue(maxsize=N)`.

**5. Database lock contention:**
- **Penyebab:** Multiple consumer workers mencoba menulis ke SQLite secara bersamaan.
- **Mitigasi:** SQLite WAL (*Write-Ahead Logging*) mode memungkinkan satu writer dan multiple readers bersamaan. `PRAGMA synchronous=NORMAL` mengurangi fsync overhead tanpa mengorbankan durabilitas secara signifikan.

**Strategi retry dan backoff:**
Untuk publisher yang mengirim event dengan retry, disarankan menggunakan **exponential backoff dengan jitter**: `delay = min(cap, base * 2^attempt) + random_jitter`. Jitter mencegah thundering herd di mana banyak publisher melakukan retry secara simultan.

*(Tanenbaum & Van Steen, 2007, Bab 6)*

---

### T7 — Eventual Consistency, Idempotency & Deduplication
*(Bab 7 — Tanenbaum & Van Steen, 2007)*

Tanenbaum & Van Steen (2007, Bab 7) mendefinisikan **eventual consistency** sebagai jaminan bahwa jika tidak ada update baru, semua replika akan konvergen ke nilai yang sama. Dalam konteks log aggregator, eventual consistency berarti: meskipun ada duplikasi sementara di berbagai tahap pipeline (publisher buffer, network, queue), *state akhir* yang tersimpan di DedupStore akan selalu konsisten — setiap event unik terepresentasi tepat sekali.

**Bagaimana idempotency membantu:**

Idempotency adalah properti operasi di mana eksekusi berulang menghasilkan state yang identik. Dalam aggregator:
- `mark_processed(event)` dapat dipanggil 1× atau 100× dengan event yang sama — state akhir di database selalu sama: satu baris dengan `(topic, event_id)` tersebut.
- Tidak ada "intermediate state" yang tidak konsisten karena `INSERT OR IGNORE` adalah operasi atomik di SQLite.

**Bagaimana deduplication membantu:**

Deduplication bertindak sebagai **convergence mechanism** yang memastikan bahwa meskipun event yang sama dikirim oleh banyak publisher atau di-retry berkali-kali, state akhir yang ter-persist selalu merepresentasikan set event unik. Ini ekuivalen dengan operasi `SET UNION` yang bersifat idempoten: `S ∪ {e} ∪ {e} = S ∪ {e}`.

**Model konsistensi yang dicapai:**

Sistem ini mencapai **read-your-own-writes consistency** dalam satu node: setelah `POST /publish` berhasil dan consumer menyelesaikan pemrosesan, `GET /events` akan mengembalikan event tersebut. Namun, karena ada latensi antara enqueue dan consume (asinkron), `GET /events` secara default memanggil `queue_manager.drain()` dengan timeout 2 detik untuk meminimalkan gap.

**Trade-off konsistensi:**

In-memory stats counter (`received`, `duplicate_dropped`) akan reset saat restart, sementara `unique_processed` selalu diambil dari SQLite (sumber kebenaran tunggal). Ini adalah bentuk **split-brain avoidance** sederhana: data yang persisten (DB) mendominasi data ephemeral (RAM counter).

*(Tanenbaum & Van Steen, 2007, Bab 7)*

---

### T8 — Metrik Evaluasi Sistem & Kaitannya dengan Keputusan Desain
*(Bab 1–7 — Tanenbaum & Van Steen, 2007)*

Evaluasi sistem terdistribusi membutuhkan metrik yang mencerminkan tujuan desain. Berdasarkan Tanenbaum & Van Steen (2007), metrik berikut dipilih beserta implikasinya:

**1. Throughput (events/detik)**
- **Definisi:** Jumlah event unik yang berhasil diproses per satuan waktu.
- **Pengukuran:** `unique_processed / uptime_seconds` dari `GET /stats`.
- **Keputusan desain:** Penggunaan batch publish dan 2 consumer worker meningkatkan throughput. SQLite WAL mode menghindari bottleneck write serialization.
- **Target:** ≥ 5.000 event/30 detik (sesuai spesifikasi).

**2. End-to-end Latency (milidetik)**
- **Definisi:** Waktu dari `POST /publish` hingga event tersedia di `GET /events`.
- **Komponen:** HTTP parsing + enqueue + dequeue + SQLite write + optional drain wait.
- **Keputusan desain:** asyncio (non-blocking I/O) meminimalkan latency di lapisan HTTP. Drain timeout 2 detik memberikan upper bound pada latensi observasi.

**3. Duplicate Rate (%)**
- **Definisi:** `duplicate_dropped / received × 100%`.
- **Pengukuran:** Tersedia langsung di `GET /stats`.
- **Keputusan desain:** Metrik ini penting untuk mendeteksi misconfigured publisher (yang mungkin melakukan retry berlebihan) dan memvalidasi bahwa dedup store bekerja benar.

**4. Dedup Store Hit Latency (milidetik)**
- **Definisi:** Waktu `INSERT OR IGNORE` per event di SQLite.
- **Keputusan desain:** `PRAGMA synchronous=NORMAL` dan WAL mode memberikan keseimbangan antara durabilitas dan performa. Pada uji stres 5.000 event, total waktu < 30 detik.

**5. Queue Depth (events)**
- **Definisi:** `queue_size` dari `GET /stats`.
- **Keputusan desain:** Queue depth yang terus meningkat mengindikasikan consumer lebih lambat dari publisher (backpressure scenario). Metrik ini memungkinkan operator menambah consumer worker secara dinamis.

**6. Error Rate (%)**
- **Definisi:** Persentase event yang gagal divalidasi schema atau gagal di-persist.
- **Keputusan desain:** Validasi Pydantic di lapisan HTTP memastikan hanya event valid yang masuk ke queue, mengurangi error di lapisan consumer.

**Kaitan ke keputusan desain (Bab 1–7):**
- Throughput tinggi → asyncio + batch (Bab 3 Communication)
- Low latency dedup → SQLite WAL + atomic INSERT (Bab 7 Consistency)
- Zero data loss → persistent volume + at-least-once (Bab 6 Fault Tolerance)
- Collision-resistant ID → UUID v4 (Bab 4 Naming)
- Partial ordering → timestamp per event (Bab 5 Synchronization)

*(Tanenbaum & Van Steen, 2007, Bab 1–7)*

---

## 3. Keputusan Desain Implementasi

### 3.1 Idempotency via `INSERT OR IGNORE`

SQLite `INSERT OR IGNORE` dengan primary key `(topic, event_id)` adalah mekanisme yang atomik, efisien, dan tidak memerlukan locking eksplisit dari sisi aplikasi. Alternatif seperti SELECT-then-INSERT rentan terhadap race condition (*TOCTOU — Time-of-Check-to-Time-of-Use*) ketika multiple consumer worker berjalan bersamaan.

```sql
INSERT OR IGNORE INTO processed_events (topic, event_id, ...) VALUES (?, ?, ...);
-- rowcount == 1 → event baru
-- rowcount == 0 → duplikat
```

### 3.2 SQLite WAL Mode

Write-Ahead Logging (WAL) memungkinkan:
- **Concurrent reads** tanpa memblokir write.
- **Faster writes** karena append-only ke WAL file, bukan synchronous write ke main DB file.
- **Crash safety** — SQLite secara otomatis me-recover state setelah crash dengan me-replay WAL.

### 3.3 asyncio.Queue sebagai Internal Broker

`asyncio.Queue` dipilih sebagai broker internal karena:
- Zero external dependency.
- Native integration dengan FastAPI (yang berbasis Starlette/asyncio).
- Support `task_done()` dan `join()` untuk drain semantics.
- Untuk skala yang lebih besar, dapat diganti dengan Redis Streams atau RabbitMQ tanpa mengubah interface `QueueManager`.

### 3.4 Ordering: Partial vs Total

Seperti dijelaskan di T5, **total ordering tidak diperlukan** untuk use case log aggregator. Event diproses berdasarkan urutan kedatangan di queue (FIFO per worker) dan disimpan dengan `processed_at` timestamp. Consumer yang membutuhkan ordering dapat men-sort berdasarkan `event.timestamp` setelah fetching dari `GET /events`.

### 3.5 Retry & Backoff (Sisi Publisher)

Publisher simulator (`src/publisher.py`) tidak mengimplementasikan retry eksplisit, namun mensimulasikan at-least-once dengan mengirim duplikat secara eksplisit. Untuk publisher produksi, rekomendasi adalah:
- Exponential backoff: `delay = min(60, 0.5 * 2^attempt)` detik.
- Maximum retry attempts: 5.
- Idempotent event_id: gunakan UUID yang sama untuk setiap retry attempt event yang sama.

---

## 4. Analisis Performa & Metrik

### 4.1 Hasil Uji Stres (Unit Test #10)

| Metrik | Nilai |
|---|---|
| Total events (unique) | 5.000 |
| Duplikat yang disuntikkan | 1.250 (25%) |
| Total events dikirim | 6.250 |
| Unique tersimpan di DB | 5.000 |
| Duplikat tertolak | 1.250 |
| Akurasi deduplication | 100% |
| Waktu eksekusi | < 30 detik |
| Throughput (insert) | ~400–600 events/detik |

### 4.2 Catatan Performa

- Bottleneck utama adalah SQLite write throughput. Untuk throughput lebih tinggi, dapat menggunakan connection pooling atau batching write ke SQLite.
- asyncio.Queue tidak mengalami backpressure pada uji dengan 6.250 event karena consumer dapat mengikuti laju publisher.
- Overhead Pydantic validation pada skema event diabaikan (< 0.1ms per event).

---

## 5. Keterkaitan ke Bab 1–7

| Bab | Konsep | Implementasi |
|---|---|---|
| Bab 1 — Intro | Karakteristik sistem terdistribusi | Trade-off consistency vs availability, resource sharing via topic |
| Bab 2 — Arsitektur | Pub-Sub pattern, middleware | asyncio.Queue sebagai internal message broker |
| Bab 3 — Komunikasi | At-least-once delivery, RPC | HTTP REST API, batch publish, retry simulation |
| Bab 4 — Penamaan | Naming scheme, collision resistance | `topic.subdomain` + UUID v4 `event_id` |
| Bab 5 — Sinkronisasi | Partial vs total ordering, Lamport clock | ISO 8601 timestamp + `processed_at` kolom di DB |
| Bab 6 — Fault Tolerance | Crash recovery, duplikasi, retry | SQLite WAL persistence, `INSERT OR IGNORE`, logging |
| Bab 7 — Konsistensi | Eventual consistency, idempotency | DedupStore sebagai single source of truth |

---

## 6. Referensi

Tanenbaum, A. S., & Van Steen, M. (2007). *Distributed systems: Principles and paradigms* (2nd ed.). Pearson Prentice Hall.

---

*Laporan ini disiapkan untuk memenuhi persyaratan UTS Sistem Terdistribusi dan Parallel.*
