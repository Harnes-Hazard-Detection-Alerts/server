# Tools server

Alat CLI pendamping server — sudah diimplementasikan:

- `replay_video.py <video.mp4> [--fps N] [--annotate out.mp4] [--arm]` — deteksi
  model + evaluator zona lewat jalur pipeline yang sama dengan server, tanpa
  HTTP/DB; `--annotate` menulis video beranotasi (identitas kelas lihat
  `models/README.md`). `--calib <file.calib.json> [--zone-radius N]` mengaktifkan
  zona meter (radius default 4 m) seperti server.
- `events_client.py --username <hse> --password '<password>'` — simulasi app
  HSE: login ke server yang sedang berjalan lalu stream SSE `/events`. Butuh
  server + PostgreSQL aktif; tool ini sendiri tak mengakses database (login via
  HTTP).
- `seed_demo.py [--scenario dev|real] [--add] [--video-dir DIR]` — **satu-satunya
  sumber data** (keputusan 12 Nov 2026): akun HSE `anything` + operator
  budi/sari/dewi selalu dibuat; `dev` menambah kamera simulasi dari folder
  video, katalog + link unit, status operasional (assignment/sesi/shift +
  Start Operating), dan riwayat alert; `real` berhenti di akun saja — kamera
  (input RTSP) dan unit ditambahkan HSE lewat app. Idempoten; `--add`
  menambah batch alert baru; `--video-dir` kosong berarti kamera tidak
  didaftarkan. Dipanggil `Scripts/reset-database.sh`, dan oleh
  `start-site-server.sh` hanya bila env `HARNES_SEED_SCENARIO` eksplisit
  (dev|real|prod — default start: TIDAK menyeed).
- `reset_database.py --scenario dev|real [--video-dir DIR] [--start]`
  (dipanggil `Scripts/reset-database.sh dev|real [--start]`) — **satu-satunya
  alat reset**: drop semua tabel domain → migrasi ulang dari file pertama →
  seed sesuai skenario. Menolak berjalan bila server masih hidup (`pgrep` +
  `pg_stat_activity`). `--start` melanjutkan ke `start-site-server.sh`
  (skenario seed disinkronkan via `HARNES_SEED_SCENARIO`; kamera auto-start —
  preview & alert langsung hidup).
