# Server Mac lokal

Jalankan dari root repository:

```sh
sh AppleApp/Scripts/start-local.sh --port 8080
```

`start-local.sh` menyiapkan dependensi dan database sebelum menjalankan server.
Untuk menjalankan hanya kerangka server tanpa setup database, gunakan `run-server-local.sh`.

Server menyediakan `/health`, `/auth/*`, `/ws` (kanal live target), `/events` (SSE
transisi), `GET/POST /users` + `PUT /users/{id}/password` + `PATCH /users/{id}`
(kelola akun oleh HSE), `/cameras*` (CRUD + start/stop + snapshot + MJPEG),
`/equipment*` (katalog unit, available-cameras, status, radius per unit, label),
`/alerts*`, `/me/alerts`, `/me/assignment`, `/me/equipment` + snapshot unit,
`POST /me/device`, `GET|DELETE /me/devices*`, `GET|POST|DELETE /assignments*`,
`PUT /me/shift`, `PUT /me/operation`, dan `POST /test/alert`. Assignment adalah
otorisasi server; routing alert memakai binding shift runtime yang wajib cocok
dengan assignment aktif.
Jalankan backend dengan Python dari `.venv`. Autentikasi dan stream memerlukan PostgreSQL; `/health` tetap tersedia tanpa query database.

## Kode

- `Sources/core/`: domain murni — kontrak identitas kelas (`Detection`, classId 0/1/2, `CLASS_NAMES`), evaluator transisi zona `RiskEvaluator` (dua mode: **meter** radius confirm 4 m / clear 6 m via kalibrasi ter-inject dan fallback **piksel** box excavator di-inflate 25% — keputusan owner 8 Nov 2026), zona ground-plane `core/zones.py`, state machine histeresis waktu `core/hazard.py` (konfirmasi 0,25 s / clear 0,5 s), port `Detector` (core/ports.py), dan `GroundCalibrationV2` kalibrasi homography piksel ↔ meter (minimal 4 titik, rekomendasi ≥6 agar ada RMS holdout; health-check menolak kolinear/ill-conditioned); modul ini tidak boleh mengimpor modul Sources lain (keputusan arsitektur clean-lite di `CONTEXT.md`, 7 Nov 2026). Identitas equipment antar-frame (tracking) menyusul bersama modulnya.
- `Sources/main.py`: entry point dan perakitan server.
- `Sources/config.py`: argumen CLI (`--port`, `--host`, `--video*`, `--calib`, `--zone-radius`; env `ZONE_RADIUS_M`).
- `Sources/api/`: handler HTTP dan streaming.
- `Sources/alerts/`: event bus, riwayat/routing shift, dispatcher nonblocking, serta
  provider APNs HTTP/2 dengan JWT ES256 yang digunakan ulang selama 50 menit.
- `Sources/detection/`: model YOLO v8 3-kelas (ultralytics, lazy import) dengan filter kelas di inference — default hanya excavator+person (arm disembunyikan; urutan kelas model: 0=excavator, 1=arm, 2=person).
- `Sources/risk/`: API publik evaluator lama (delegator tipis ke `Sources/core/evaluator.py`; dipertahankan untuk Tools, tests, dan shim `detection.py`).
- `Sources/cameras/`: kamera = sumber frame yang diproses server (✅ berjalan): CRUD + start/stop playback multi-kamera (satu thread per kamera) + snapshot JPEG raw/annotated (ETag per frame) + feed MJPEG `stream.mjpeg`; `recording` (file) dan `rtsp` (live, sejak 11 Nov — 3× open gagal beruntun → status `error` + healthNote `fallback`; kembali start untuk pulih) sama-sama diproses; CLI `python -m Sources.cameras.cli` (add/list/remove) + flag `--auto-start-cameras`. Frame masuk pipeline normal.
- `Sources/video_input/`: pembaca video rekaman + thread playback realtime ke `AlertServer.process_frame`.
- `Sources/auth/`: hashing password scrypt; kebijakan sesi demo — 365 hari default
  (`HARNES_SESSION_DAYS`), rate-limit login opsional (`HARNES_LOGIN_LIMIT=1`), sesi
  operator non-eksklusif (`HARNES_OPERATOR_SESSIONS_EXCLUSIVE=1` mengembalikan perilaku
  produksi satu sesi).
- `Sources/users/`: perintah lokal pembuatan akun operator/HSE (CLI tanpa signup publik) + `directory.py` — direktori operator untuk HSE (jumlah assignment aktif, device online).
- `Sources/assignments/`: sumber kebenaran penugasan excavator↔operator. HSE membuat,
  mengganti, atau mengakhiri assignment; server membatasi satu assignment aktif per
  unit dan per operator.
- `Sources/devices/`: CRUD perangkat lama diparkir; registrasi push aktif memakai
  `/me/device`, sedangkan `/me/shift` menjadi sumber routing operator.
- `Sources/equipment/`: katalog unit — pendaftaran unit dari kamera (`registry.py`),
  nama tampilan + nomor kabin (`labels.py`), radius Safety Zone per unit (`zone.py`),
  dan status operasi (`operation.py`).
- `Sources/infrastructure/database/`: provisioning lokal, koneksi, migrasi, dan backup PostgreSQL.
- `migrations/`: SQL berversi untuk `users`, `sessions`, `assignments`, `devices`, `alerts` (+ `alert_deliveries`), `cameras`.
- `models/`: bobot model deteksi ikut repo (`best_v8_3class.pt`, 5,3 MiB) + dokumentasi versi/kelas/kalibrasinya.
- `Tools/`: `replay_video.py` (deteksi CLI tanpa server, opsi video beranotasi) dan `events_client.py` (simulasi app HSE menerima alert lewat SSE).
- `tests/`: pemeriksaan perilaku server + pengawas arsitektur clean-lite (`test_architecture.py`, tanpa dependensi tambahan).
- `requirements-ml.txt`: dependensi deteksi (torch/ultralytics/opencv) — opsional, dipasang terpisah dari `requirements.txt`.
- `requirements-dev.txt` + `.importlinter`: kontrak arsitektur untuk pemeriksaan grafik import penuh (dev-only; `lint-imports` dari Server).

`server.py` dan `detection.py` di root hanya mempertahankan entry point/import lama.
Kode fitur berada di `Sources/`.

### Mengirim alert uji ke iPhone

Aktifkan `ENABLE_TEST_ALERTS=1` hanya saat pengujian, login sebagai HSE, lalu kirim:

```sh
curl -X POST http://MacBook-Pro-Jeany.local:8080/test/alert \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"equipmentInstanceId":"unit-01","state":"danger"}'
```

Gunakan `"state":"safe"` untuk mengirim `zone_cleared`. Endpoint ini melewati pipeline
routing/history/WebSocket/APNs yang sama dengan hasil deteksi dan mengembalikan 404 ketika
flag tidak diaktifkan.

## Batas implementasi

Deteksi v8 dan pembaca video sudah tersambung ke `process_frame`. Evaluasi zona punya dua mode:
`--calib <file.calib.json>` mengaktifkan zona meter dengan radius confirm default **4 m** (keputusan owner 8 Nov 2026; ubah lewat `--zone-radius` atau env `ZONE_RADIUS_M`) dan radius clear 6 m (histeresis jarak); tanpa kalibrasi, evaluator fallback ke zona piksel (box excavator di-inflate 25%). Transisi status dikontrol histeresis waktu (core/hazard.py): konfirmasi butuh kehadiran ≥ 0,25 s dan clear butuh ketidakhadiran ≥ 0,5 s — satu frame tunggal tidak pernah mengubah status. Alert masih bertanda
`simulation: true` sampai identitas equipment tracking final dan kontrak tim disepakati. Riwayat alert berjalan. Assignment permanen sekarang tersimpan di server; binding shift tetap data runtime dan hanya boleh dibuat untuk assignment operator yang aktif. Modul kamera (termasuk `location`), event typed (`zoneStatus` tri-level + `detectionCounts`), zona high-risk ½ radius (`person_high_risk`, mode meter), evidence burst, serta **kelola akun operator dari app** (`POST /users`, `PUT /users/{id}/password`, `PATCH /users/{id}` — reset password mencabut sesi target; nonaktif mengakhiri assignment aktif serta membersihkan device_tokens + shift_bindings; guard anti-nonaktifkan-akun-sendiri) berjalan. M7 berjalan: token device + binding shift, routing `/me/alerts`, WebSocket `/ws`, dan APNs HTTP/2/JWT dengan worker nonblocking; SSE tetap tersedia selama transisi. Yang belum: identitas equipment antar-frame (adapter tracking nyata — `trackId` sudah siap dipakai evaluator), key `.p8` APNs + pembuktian locked-screen pada perangkat fisik (Critical Alerts bukan bagian implementasi saat ini), dan klip evidence (tahap lanjut dari burst JPEG).
PostgreSQL 18 berjalan langsung di Mac tanpa Docker. Setup membuat `.env` lokal yang dibaca modul database.

## Memasukkan video sebagai CCTV (multi-kamera)

**Jalur cepat tim** — dua langkah terpisah, tidak tumpang-tindih:

1. **Data skenario** (seed + registrasi kamera simulasi): `Scripts/reset-database.sh dev|real`
   — satu-satunya pintu seed; `Tools/seed_demo.py` tetap SATU sumber pembuatan data.
   Untuk `dev`, video sampel yang belum ada diunduh otomatis (lewat `gh`) ke
   `AppleApp/Server/recordings/` (folder terkait; sudah di-gitignore), dan path yang
   terdaftar di PostgreSQL (`cameras.source`) = path absolut file di folder itu —
   **tanpa copy/cut/paste manual**. Unduhan berikutnya otomatis dilewati. Video sendiri
   tetap bisa: taruh di folder itu (awalan nama `pexels-…` sesuai daftar di
   `Tools/seed_demo.py`) atau lewat env `HARNES_VIDEO_DIR` ke folder lain — path
   override di-absolutkan sebelum didaftarkan. **Jangan pindah/hapus file selagi
   kameranya terdaftar** (`python -m Sources.cameras.cli list` untuk melihat);
   dipindah berarti daftar ulang.
2. **Start server** (murni — tanpa seed/unduhan; bind default `0.0.0.0` agar iPhone
   fisik di LAN dan simulator sama-sama bisa akses):

```sh
sh AppleApp/Scripts/start-site-server.sh
# env opsional: HARNES_SERVER_PORT (default 8080) · HARNES_SERVER_HOST (default 0.0.0.0;
#               127.0.0.1 = loopback saja) · HARNES_SEED_SCENARIO=dev|real|prod (seed
#               eksplisit saat start — jarang perlu; default TIDAK menyeed)
```

Cara manual — dari `AppleApp/Server`, kapan pun, tanpa menjalankan server:

```sh
.venv/bin/python -m Sources.cameras.cli add /Volumes/rekaman/site-timur.mp4 \
    --name "Site Timur" --location "Main Entrance" --fps 8 --loop
.venv/bin/python -m Sources.cameras.cli list          # lihat id + status
.venv/bin/python -m Sources.cameras.cli remove <id>   # hapus (id penuh atau prefiks unik)
```

Alternatif RTSP: video rekaman juga bisa disiarkan sebagai stream RTSP loop
lewat `Scripts/start-video-rtsp.sh <nama-singkat> [file]` (broker MediaMTX
sama), dan webcam/iPhone lewat `Scripts/start-mac-cam-rtsp.sh` — keduanya
mencetak link `rtsp://…:8554/<nama>` siap diinput ke app iOS (sumber
`cameras.source` rtsp:// diproses `RTSPReader` seperti CCTV IP). Pendaftaran
kamera di app HSE; CLI di atas tetap tersedia untuk pendaftaran non-app.

Nyalakan/gerakan kamera: register via app/API **langsung streaming**
(auto-start register, 13 Nov; produser RTSP belum siap → 3× gagal → status
`error`/healthNote `fallback`, nyalakan ulang dari app). Deteksi model per
kamera digate status unit-nya (13 Nov): hanya jalan bila ada operator yang
sedang Start Operating — cache TTL 5 dtk, gagal-baca-DB = fail-open; kamera
idle streaming tanpa biaya MPS dan snapshot raw tetap tersedia. `POST /cameras/{id}/start` dan `/stop` tetap tersedia untuk nyalakan
ulang dan mematikan; kamera daftar-CLI mulai idle. Flag lama tetap jalan
untuk semua-kamera-saat-boot bila mau:

```sh
.venv/bin/python -m Sources.main --auto-start-cameras --port 8080
# atau lewat script: HARNES_AUTO_START_CAMERAS=1 sh AppleApp/Scripts/start-site-server.sh
```

Kamera yang dijalankan masuk
pipeline penuh (deteksi → zona → alert → riwayat); `--video <path>` lama tetap tersedia
untuk satu sumber ad-hoc tanpa registrasi.
App iOS terintegrasi (login, registrasi push, binding shift, monitoring; alur lengkap:
`Docs/team-runbook.md`). Server default ke loopback untuk HTTP lokal; penggunaan jaringan memerlukan HTTPS.
Lihat [setup lokal](../Docs/local-development.md) dan [kontrak](../Contracts/README.md).

## Deteksi model & simulasi realtime (CLI, tanpa UI)

Model v8 sudah ikut repo (`models/best_v8_3class.pt`) dan dimuat otomatis —
tidak ada konfigurasi model yang wajib. Yang perlu dipasang hanya dependensinya:

1. Pasang dependensi ML (satu kali):
   ```sh
   .venv/bin/python -m pip install -r requirements-ml.txt
   ```
   Kunci `DETECTION_*` di `.env` (lihat `.env.example`) hanya untuk menimpa default.
2. Terminal 1 — server + video rekaman (deteksi realtime, status per frame di CLI):
   ```sh
   sh AppleApp/Scripts/run-server-local.sh --video <path>.mp4 --video-fps 8 --video-loop
   curl -s localhost:8080/health                  # "modelLoaded": true
   ```
   Zona meter: tambahkan `--calib <path.calib.json>` (hasil `GroundCalibrationV2` minimal 6 titik) dan
   opsional `--zone-radius 4` (default 4 m; env `ZONE_RADIUS_M` berlaku juga).
3. Terminal 2 — simulasi app menerima alert lewat SSE (akun role hse):
   ```sh
   .venv/bin/python Tools/events_client.py --username <hse> --password '<password>'
   ```
`Tools/replay_video.py <video.mp4> [--annotate out.mp4] [--arm]` menjalankan deteksi tanpa
server/DB untuk pengecekan cepat model + zona placeholder.
