# HARNES Server

Local Python server for the HARNES iOS app — hazard-zone detection (YOLOv8), camera management, and real-time alerts (WebSocket / SSE / APNs).

> **For Apple Reviewers — start here.** You do **not** need a Mac. The server runs on **macOS, Linux, or Windows (WSL)**. If you cannot run a local server, use **Option A (hosted)** below — no setup. For full CCTV simulation, use **Option B (local)** — one command with Docker or Python.

---

## Quick Start for Reviewers

### Option A — No setup (hosted demo server)

If the submission provides a hosted URL in **App Review Notes**, you do not need to run anything locally:

1. Open **HARNES** on iPhone.
2. On **Login**, tap **Server Address** and enter the hosted URL from App Review Notes, e.g.:
   ```
   https://harnes-demo.example.com/
   ```
3. Sign in with a demo account (see table below).

> The hosted server is the same codebase as this repo, pre-seeded with demo cameras and alerts. If no hosted URL is listed, use Option B.

### Option B — Run locally (any OS, ~3 minutes)

Works on **macOS, Linux, Windows (WSL)**. Requires **Python 3.11+** and **PostgreSQL 14+** *or* **Docker**.

#### B1 — With Docker (simplest, any OS)

```sh
git clone https://github.com/akhnafal-aban/HARNES-Server.git
cd HARNES-Server

# Fetch sample CCTV videos (~64 MB, Pexels free-license, one-time)
# For private repos: `gh auth login` first, then:
python3 Tools/fetch_recordings.py   # or: gh release download recordings-720p --repo akhnafal-aban/HARNES-Server --pattern "*720p*" --dir recordings

docker compose up --build
# → Postgres + server start, DB seeded with 3 cameras, 3 excavators, 3 operators, alert history
# → Listening on http://localhost:8080
```

#### B2 — Without Docker (Python + Postgres)

```sh
git clone https://github.com/akhnafal-aban/HARNES-Server.git
cd HARNES-Server

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

python3 Tools/fetch_recordings.py
.venv/bin/python Tools/reset_database.py --scenario dev --video-dir recordings

# Bind to all interfaces so a physical iPhone on the same Wi-Fi can reach it:
.venv/bin/python -m Sources.main --host 0.0.0.0 --port 8080 --auto-start-cameras
```

You should see:

```
Alert server listening on port 8080
[risk] zona piksel (fallback tanpa kalibrasi)
```

> **No ML setup needed.** Object detection (`requirements-ml.txt` + `models/best_v8_3class.pt` 5.3 MiB) is optional. Without it the server still runs — camera feeds and alerts use the seeded demo data. The model loads automatically when ML deps are installed.

### Verify it is running

```sh
curl http://localhost:8080/health
# {"status":"ok","modelLoaded": false}  ← ok even without ML deps
```

Find your machine's LAN address for a **physical iPhone** (both on same Wi-Fi):

```sh
# macOS / Linux
ipconfig getifaddr en0 2>/dev/null || hostname -I | awk '{print $1}'
# Windows (PowerShell)
ipconfig | findstr IPv4
# mDNS (Mac)
scutil --ncols 1 --get LocalHostName  # e.g. MacAban.local
```

For **iOS Simulator** on the same machine, `http://localhost:8080/` works.

### Connect the iOS app

1. Open **HARNES** on iPhone / Simulator.
2. On **Login**, tap **Server Address** (top field).
3. Enter the URL — **must include scheme and port, with trailing slash**:
   ```
   http://192.168.1.42:8080/
   ```
   or `http://localhost:8080/` (Simulator) or `https://harnes-demo.example.com/` (hosted)
4. Enter a demo account and tap **Sign In**.

> **Release vs Debug:** The App Store build (`Release`) only allows `https://`. The `TestFlight` / `Debug` build also allows `http://` for private addresses (`localhost`, `*.local`, `10.*`, `192.168.*`, `172.16-31.*`). Reviewers on LAN should use the TestFlight build.

### Demo accounts

Seeded by `Tools/seed_demo.py --scenario dev` (or `reset_database.py --scenario dev`). Active for **7 days**.

| Username | Password | Role | Can verify |
|---|---|---|---|
| `anything` | `anythingcuy06` | **HSE** | Cameras, fleet, operator directory, assignments, all alerts |
| `budi` | `password-demo-2026` | Operator | Shift binding, Start Operating, personal alerts |
| `sari` | `password-demo-2026` | Operator | Same |
| `dewi` | `password-demo-2026` | Operator | Same |

After login:

- **HSE (`anything`)** — 3 demo cameras + 3 excavators already **Operating**, alert history and live WebSocket/SSE without extra steps.
- **Operator (`budi`/`sari`/`dewi`)** — already assigned & operating; verify `Start/Stop Operating`, `/me/alerts`, device registration.

### What the reviewer sees after seeding

`--scenario dev --video-dir recordings` creates:
- **3 cameras** — `Site Timur / Barat / Depo` from `recordings/*720p.mp4` (loop, 8 fps, auto-started) at `GET /cameras` and in-app **HSE → Cameras**. Each supports `snapshot` (JPEG) and `stream.mjpeg`.
- **3 excavators** — `Excavator - 1..3`, auto-linked and already **Start Operating**
- **3 operators** — each bound + live session, so the operation gate is open and alerts flow
- **Alert history** — ~15 incidents over 72h, at `GET /alerts` and live via `GET /events` (SSE) / `GET /ws` (WebSocket)

Without `recordings/`, the same seed creates accounts + equipment but **no cameras** — HSE can still add a camera from the app (RTSP URL or file) and the fleet appears instantly. No manual DB edits needed.

### Sending a test alert (optional)

With `ENABLE_TEST_ALERTS=1` the server exposes `POST /test/alert` (same pipeline as real detections):

```sh
ENABLE_TEST_ALERTS=1 .venv/bin/python -m Sources.main --host 0.0.0.0 --port 8080 &
TOKEN=$(curl -s -X POST http://localhost:8080/auth/login -H 'Content-Type: application/json' -d '{"username":"anything","password":"anythingcuy06"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['accessToken'])")
curl -X POST http://localhost:8080/test/alert -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"equipmentInstanceId":"<id>-eq-1","state":"danger"}'
```

### Stopping / resetting

```sh
# Docker:
docker compose down
# Fresh with cameras:
docker compose up --build   # re-seeds automatically

# Python:
# Ctrl+C to stop
.venv/bin/python Tools/reset_database.py --scenario dev --video-dir recordings  # with cameras
.venv/bin/python Tools/reset_database.py --scenario real                        # accounts only
```

---

## Endpoints

| Group | Method & Path | Notes |
|---|---|---|
| Health | `GET /health` | No auth; `modelLoaded` true when YOLO deps installed |
| Auth | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | Bearer token, 7-day demo sessions |
| Cameras | `GET/POST /cameras`, `POST /cameras/{id}/start\|stop`, `GET /cameras/{id}/snapshot`, `GET /cameras/{id}/stream.mjpeg` | Recording + RTSP; auto-start on register |
| Equipment | `GET /equipment*`, `GET /equipment/{id}/available-cameras` | Fleet registry, Safety Zone radius, cabin labels |
| Users (HSE) | `GET/POST /users`, `PUT /users/{id}/password`, `PATCH /users/{id}` | Create/reset/disable operators |
| Assignments | `GET/POST/DELETE /assignments` | One active per unit and per operator |
| Operator | `GET /me/alerts`, `GET /me/assignment`, `GET /me/equipment`, `PUT /me/shift`, `PUT /me/operation`, `POST /me/device` | Shift + operation gate |
| Alerts | `GET /alerts`, `GET /events` (SSE), `GET /ws` (WebSocket) | Typed `zoneStatus` + `detectionCounts` |
| Test | `POST /test/alert` | Only when `ENABLE_TEST_ALERTS=1` |

Full contract in `migrations/` and `Sources/api/handlers.py`.

---

## Configuration

All keys optional — defaults work for review. Override via `.env` (see `.env.example`) or env vars.

| Key | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://safety_app:…@localhost:5432/safety_system` | Set by `reset_database.py` or `docker-compose.yml` |
| `ZONE_RADIUS_M` | `4` | Safety zone confirm radius (m); clear 6 m |
| `DETECTION_MODEL_PATH` | `models/best_v8_3class.pt` | Bundled YOLOv8 3-class |
| `DETECTION_CONF` | `0.40` | Confidence threshold |
| `EVIDENCE_DIR` | system temp | Burst JPEG evidence |
| `APNS_*` | `mock` | Push — mock by default |
| `ENABLE_TEST_ALERTS` | `0` | Enable `POST /test/alert` |
| `--host` / `--port` | `127.0.0.1:8080` | Use `0.0.0.0` for LAN |
| `--calib` | — | `GroundCalibrationV2` `.calib.json` for meter zones |
| `--auto-start-cameras` | off | Start recording cameras on boot |

ML deps (optional):

```sh
pip install -r requirements-ml.txt
```

---

## Architecture (brief)

```
Sources/core/          Pure domain — RiskEvaluator (meter 4m/6m or pixel fallback), GroundCalibrationV2, hazard hysteresis
Sources/detection/     YOLOv8 wrapper (lazy import)
Sources/cameras/       CRUD + per-camera thread + MJPEG/RTSP + supervisor
Sources/api/           HTTP + WebSocket
Sources/alerts/        Event bus + APNs HTTP/2 JWT
Sources/assignments/   Single active per unit/operator
Sources/equipment/     Fleet registry, zone radius, operation status
Sources/infrastructure/database/  Provisioning, migrations, backups
migrations/            Versioned SQL
models/                Bundled weights (5.3 MiB)
Tools/                 fetch_recordings.py, seed_demo.py, reset_database.py
tests/                 Behaviour + import-linter guard
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Connection refused` from iPhone | Same Wi-Fi; `--host 0.0.0.0`; use `http://<LAN-IP>:8080/` not `localhost` (physical device) |
| `invalidURL` on login | Include `http://` and trailing `/`; private IPs only on Debug/TestFlight |
| `No cameras` after seed | Run `Tools/fetch_recordings.py` first, then `reset_database.py --video-dir recordings`; or add RTSP via HSE → Cameras |
| `401` / expired | Re-login; demo sessions 7 days (`HARNES_SEED_SESSION_DAYS`) |
| `modelLoaded: false` | Expected without `requirements-ml.txt`; demo still works |
| `fetch_recordings.py` 404 | Private repo — `gh auth login` first; or `gh release download recordings-720p --repo akhnafal-aban/HARNES-Server --pattern "*720p*" --dir recordings` |
| Postgres not running | `docker compose up db` or `brew services start postgresql@14` / `sudo service postgresql start` |

---

## License

Internal use for HARNES App Review and development. Do not commit `.env`, `.secrets/`, `recordings/`, or `*.p8` keys.
