# HARNES Server

Python backend for the HARNES iOS app. YOLOv8 hazard-zone detection, camera management, and alerts over WebSocket, SSE, and APNs.

> Apple reviewers: start here. No Mac required. The server runs on macOS, Linux, and Windows (WSL). If you cannot run anything locally, use the hosted URL from App Review Notes. To test CCTV playback end to end, run it locally. Both paths are below.

---

## How to review

### If you have a hosted URL (no setup)

When App Review Notes include a hosted URL, you don't need to start a local server:

1. Open HARNES on iPhone.
2. On Login, tap **Server Address** and enter the URL from the notes, for example:
   ```
   https://harnes-demo.example.com/
   ```
3. Sign in with a demo account from the table below.

That server is the same code as this repo, already seeded with cameras and alerts. If the notes don't list a URL, run it locally.

### Run it locally (any OS, about 3 minutes)

You need Python 3.11+ and either PostgreSQL 14+ or Docker.

**With Docker — one command:**

```sh
git clone https://github.com/akhnafal-aban/HARNES-Server.git
cd HARNES-Server

# Sample CCTV videos, about 64 MB, Pexels free license. One time only.
# Private repo: run `gh auth login` first.
python3 Tools/fetch_recordings.py
# Alternative:
# gh release download recordings-720p --repo akhnafal-aban/HARNES-Server --pattern "*720p*" --dir recordings

docker compose up --build
# Postgres and server come up, seeded with 3 cameras, 3 excavators, 3 operators, and alert history
# http://localhost:8080
```

**Without Docker:**

```sh
git clone https://github.com/akhnafal-aban/HARNES-Server.git
cd HARNES-Server

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

python3 Tools/fetch_recordings.py
.venv/bin/python Tools/reset_database.py --scenario dev --video-dir recordings

# Allow a physical iPhone on the same Wi-Fi to connect:
.venv/bin/python -m Sources.main --host 0.0.0.0 --port 8080 --auto-start-cameras
```

Expected output:

```
Alert server listening on port 8080
[risk] zona piksel (fallback tanpa kalibrasi)
```

Object detection is optional. The repo includes `models/best_v8_3class.pt` (5.3 MiB). It loads automatically if you install `requirements-ml.txt`. Without it, the seeded demo data still drives cameras and alerts.

### Check that it is running

```sh
curl http://localhost:8080/health
# {"status":"ok","modelLoaded": false}
```

For a physical iPhone, both devices must be on the same Wi-Fi. Use your machine's LAN address:

```sh
# macOS / Linux
ipconfig getifaddr en0 2>/dev/null || hostname -I | awk '{print $1}'
# Windows (PowerShell)
ipconfig | findstr IPv4
# macOS mDNS name
scutil --ncols 1 --get LocalHostName  # e.g. MacAban.local
```

The iOS Simulator can use `http://localhost:8080/` directly.

### Connect the app

1. Open HARNES on iPhone or Simulator.
2. On Login, tap **Server Address** at the top.
3. Enter the URL with scheme, port, and trailing slash:
   ```
   http://192.168.1.42:8080/
   ```
   Simulator: `http://localhost:8080/`. Hosted: `https://harnes-demo.example.com/`.
4. Enter a demo account and tap Sign In.

Release builds only accept `https://`. TestFlight and Debug builds also accept `http://` for private addresses (`localhost`, `*.local`, `10.*`, `192.168.*`, `172.16-31.*`). Reviewers on a local network should use the TestFlight build.

### Demo accounts

Seeded by `Tools/seed_demo.py --scenario dev` or `reset_database.py --scenario dev`. Each stays valid for 7 days.

| Username | Password | Role | Good for |
|---|---|---|---|
| `anything` | `anythingcuy06` | HSE | Cameras, fleet, operators, assignments, all alerts |
| `budi` | `password-demo-2026` | Operator | Shift binding, Start Operating, personal alerts |
| `sari` | `password-demo-2026` | Operator | Same |
| `dewi` | `password-demo-2026` | Operator | Same |

After signing in:

- **HSE (`anything`)** — three demo cameras and three excavators are already Operating. Alert history and live updates over WebSocket and SSE are ready.
- **Operator (`budi` / `sari` / `dewi`)** — already assigned and operating. Check Start/Stop Operating, `/me/alerts`, and device registration.

### What gets seeded

`--scenario dev --video-dir recordings` creates:

- **3 cameras** — Site Timur, Barat, and Depo from `recordings/*720p.mp4`. Loop at 8 fps, auto-started. Available at `GET /cameras` and in HSE → Cameras. Each has `snapshot` and `stream.mjpeg`.
- **3 excavators** — Excavator 1 to 3, linked to the cameras and already Start Operating.
- **3 operators** — each bound to one unit with a live session, so the operation gate is open and alerts flow.
- **Alert history** — about 15 incidents from the last 72 hours at `GET /alerts`, plus live `GET /events` (SSE) and `GET /ws` (WebSocket).

Without `recordings/`, the seed still creates accounts and equipment but no cameras. HSE can add one from the app with an RTSP URL and the fleet shows up right away. No direct database edits needed.

### Send a test alert (optional)

Enable `ENABLE_TEST_ALERTS=1` to expose `POST /test/alert`. It follows the same path as real detections:

```sh
ENABLE_TEST_ALERTS=1 .venv/bin/python -m Sources.main --host 0.0.0.0 --port 8080 &
TOKEN=$(curl -s -X POST http://localhost:8080/auth/login -H 'Content-Type: application/json' -d '{"username":"anything","password":"anythingcuy06"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['accessToken'])")
curl -X POST http://localhost:8080/test/alert -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"equipmentInstanceId":"<id>-eq-1","state":"danger"}'
```

### Stop or reset

```sh
# Docker
docker compose down
docker compose up --build   # re-seeds

# Python
# Ctrl+C to stop
.venv/bin/python Tools/reset_database.py --scenario dev --video-dir recordings  # with cameras
.venv/bin/python Tools/reset_database.py --scenario real                        # accounts only
```

---

## Endpoints

| Group | Method & Path | Notes |
|---|---|---|
| Health | `GET /health` | No auth. `modelLoaded` true when YOLO deps are present |
| Auth | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | Bearer token, 7-day demo sessions |
| Cameras | `GET/POST /cameras`, `POST /cameras/{id}/start\|stop`, `GET /cameras/{id}/snapshot`, `GET /cameras/{id}/stream.mjpeg` | Recording and RTSP, auto-start on register |
| Equipment | `GET /equipment*`, `GET /equipment/{id}/available-cameras` | Fleet, Safety Zone radius, cabin labels |
| Users (HSE) | `GET/POST /users`, `PUT /users/{id}/password`, `PATCH /users/{id}` | Create, reset, or disable operators |
| Assignments | `GET/POST/DELETE /assignments` | One active assignment per unit and per operator |
| Operator | `GET /me/alerts`, `GET /me/assignment`, `GET /me/equipment`, `PUT /me/shift`, `PUT /me/operation`, `POST /me/device` | Shift and operation gate |
| Alerts | `GET /alerts`, `GET /events` (SSE), `GET /ws` (WebSocket) | `zoneStatus` and `detectionCounts` |
| Test | `POST /test/alert` | Only when `ENABLE_TEST_ALERTS=1` |

Contract details are in `migrations/` and `Sources/api/handlers.py`.

---

## Configuration

Defaults work for review. Override with `.env` (see `.env.example`) or environment variables.

| Key | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://safety_app:…@localhost:5432/safety_system` | Set by `reset_database.py` or `docker-compose.yml` |
| `ZONE_RADIUS_M` | `4` | Confirm radius in meters. Clear is 6 m |
| `DETECTION_MODEL_PATH` | `models/best_v8_3class.pt` | Bundled YOLOv8 3-class model |
| `DETECTION_CONF` | `0.40` | Confidence threshold |
| `EVIDENCE_DIR` | system temp | Burst JPEG evidence |
| `APNS_*` | `mock` | Mock push by default |
| `ENABLE_TEST_ALERTS` | `0` | Enable `POST /test/alert` |
| `--host` / `--port` | `127.0.0.1:8080` | Use `0.0.0.0` for LAN |
| `--calib` | — | `GroundCalibrationV2` `.calib.json` |
| `--auto-start-cameras` | off | Start recording cameras on boot |

Optional ML deps:

```sh
pip install -r requirements-ml.txt
```

---

## Architecture

```
Sources/core/          RiskEvaluator (meter 4m/6m or pixel fallback), GroundCalibrationV2, hazard hysteresis
Sources/detection/     YOLOv8 wrapper, lazy import
Sources/cameras/       CRUD, per-camera thread, MJPEG/RTSP, supervisor
Sources/api/           HTTP and WebSocket
Sources/alerts/        Event bus and APNs HTTP/2 JWT
Sources/assignments/   One active assignment per unit and per operator
Sources/equipment/     Fleet, zone radius, operation status
Sources/infrastructure/database/  Provisioning, migrations, backups
migrations/            Versioned SQL
models/                Weights, 5.3 MiB
Tools/                 fetch_recordings.py, seed_demo.py, reset_database.py
tests/                 Behavior and import-linter guard
```

`Sources/core` does not import other `Sources` modules. Enforced by `.importlinter`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Connection refused` from iPhone | Same Wi-Fi, `--host 0.0.0.0`, use `http://<LAN-IP>:8080/` not `localhost` on device |
| `invalidURL` on login | Include `http://` and trailing `/`. Private IPs work only on Debug/TestFlight |
| No cameras after seed | Run `Tools/fetch_recordings.py` first, then `reset_database.py --video-dir recordings`. Or add RTSP in HSE → Cameras |
| `401` or expired | Sign in again. Demo sessions last 7 days (`HARNES_SEED_SESSION_DAYS`) |
| `modelLoaded: false` | Normal without `requirements-ml.txt`. Demo still works |
| `fetch_recordings.py` 404 | Private repo needs `gh auth login`. Alternative: `gh release download recordings-720p --repo akhnafal-aban/HARNES-Server --pattern "*720p*" --dir recordings` |
| Postgres not running | `docker compose up db`, or `brew services start postgresql@14`, or `sudo service postgresql start` |

Do not commit `.env`, `.secrets/`, `recordings/`, or `*.p8` keys.
