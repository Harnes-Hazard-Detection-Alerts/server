# HARNES Server

Local Python server for the HARNES iOS app — hazard-zone detection (YOLOv8), camera management, and real-time alerts (WebSocket / SSE / APNs).

> **For Apple Reviewers — start here.** The app is fully functional without the server running (`/health` is the only public check), but login, cameras, alerts, and assignments require this server. Follow **Quick Start for Reviewers** below — under 3 minutes on a Mac.

---

## Quick Start for Reviewers

### What you need

- **macOS** with **Python 3.11+** and **PostgreSQL 14+** installed (`brew install python@3.11 postgresql@14`)
- Both devices on the **same network** (Mac + iPhone), or use the iOS Simulator on the same Mac.

### 1. Start the server (one command)

```sh
git clone https://github.com/akhnafal-aban/HARNES-Server.git
cd HARNES-Server

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Fetch sample CCTV videos (one-time, ~64 MB, Pexels free-license):
# — requires `gh` auth for private repos, or works anonymously if repo is public
.venv/bin/python Tools/fetch_recordings.py

# Create DB + seed demo accounts/cameras (first run only):
.venv/bin/python Tools/reset_database.py --scenario dev --video-dir recordings
# If you skipped fetch_recordings, seed still works but cameras will be empty
# — add your own via app (HSE → Cameras → Add RTSP) or re-run with --video-dir.

# Run — binds to all interfaces so a physical iPhone can reach it:
.venv/bin/python -m Sources.main --host 0.0.0.0 --port 8080 --auto-start-cameras
```

You should see:

```
Alert server listening on port 8080
[risk] zona piksel (fallback tanpa kalibrasi)
```

> **No ML setup needed.** Object detection (`requirements-ml.txt` + `models/best_v8_3class.pt`) is optional. Without it the server still runs — camera feeds and alerts are driven by the seeded demo data. The model file is bundled in `models/` and loads automatically when the ML deps are installed.

### 2. Verify it is running

```sh
curl http://localhost:8080/health
# {"status":"ok","modelLoaded": false}  ← ok even without ML deps
```

Find your Mac's address for the iPhone:

```sh
ipconfig getifaddr en0          # e.g. 192.168.1.42
# or
scutil --ncols 1 --get LocalHostName  # e.g. MacAban.local
```

### 3. Connect the iOS app

1. Open **HARNES** on iPhone / Simulator.
2. On the **Login** screen, tap the **Server Address** field (top).
3. Enter your Mac's URL — **must include scheme and port**:
   ```
   http://192.168.1.42:8080/
   ```
   or
   ```
   http://MacAban.local:8080/
   ```
4. Enter a demo account (see below) and tap **Sign In**.

> **Release vs Debug builds:** The App Store build (`Release`) only allows `https://` URLs. The `TestFlight` / `Debug` build also allows `http://` for private-network addresses (`localhost`, `*.local`, `10.*`, `192.168.*`, `172.16-31.*`). Reviewers testing over LAN should use the TestFlight build.

### Demo accounts

Seeded by `Tools/seed_demo.py --scenario dev`. All accounts are active for **7 days** after seeding.

| Username | Password | Role | What you can do |
|---|---|---|---|
| `anything` | `anythingcuy06` | **HSE** | Manage operators, cameras, equipment, assignments; view all alerts |
| `budi` | `password-demo-2026` | Operator | Assigned to a demo excavator, already **Start Operating** |
| `sari` | `password-demo-2026` | Operator | Same |
| `dewi` | `password-demo-2026` | Operator | Same |

After login:

- **As HSE (`anything`)** — verify camera list, equipment fleet, operator directory, alert history, and live WebSocket/SSE events without any extra setup. Three demo cameras are pre-registered and three excavators are already operating.
- **As Operator (`budi`/`sari`/`dewi`)** — verify shift binding, `Start Operating` / `Stop`, personal alerts (`/me/alerts`), and device registration (`POST /me/device`).

### Sending a test alert (optional)

With `ENABLE_TEST_ALERTS=1` the server exposes `POST /test/alert` for manual verification. This goes through the same routing/push/WebSocket as real detections.

```sh
ENABLE_TEST_ALERTS=1 .venv/bin/python -m Sources.main --host 0.0.0.0 --port 8080 &

TOKEN=$(curl -s -X POST http://localhost:8080/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"anything","password":"anythingcuy06"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['accessToken'])")

curl -X POST http://localhost:8080/test/alert \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"equipmentInstanceId":"<id>-eq-1","state":"danger"}'   # danger | safe
```

### What the reviewer sees after seeding

`--scenario dev` creates:
- **3 cameras** — `Site Timur / Barat / Depo` from `recordings/*720p.mp4` (loop, 8 fps, auto-started), visible at `GET /cameras` and in-app **HSE → Cameras**
- **3 excavators** — one per camera (`Excavator - 1..3`), auto-linked and assigned, already **Start Operating**
- **3 operators** — `budi/sari/dewi` each bound to one unit + live session, so `PUT /me/operation` gate is open and detections flow to alerts
- **Alert history** — ~15 incidents over 72h, at `GET /alerts` and live via `GET /events` (SSE) / `GET /ws` (WebSocket)

Without `recordings/`, the same seed creates accounts + equipment but **no cameras** — HSE can still add a camera from the app (`RTSP` or file path) and the fleet appears instantly. No manual DB edits needed.

### Stopping / resetting

```sh
# Stop: Ctrl+C in the server terminal

# Fresh reset (drops DB, re-migrates, re-seeds with cameras):
.venv/bin/python Tools/reset_database.py --scenario dev --video-dir recordings

# Without cameras (accounts only):
.venv/bin/python Tools/reset_database.py --scenario real
```

---

## Endpoints

| Group | Method & Path | Notes |
|---|---|---|
| Health | `GET /health` | No auth; `modelLoaded` true when YOLO deps installed |
| Auth | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | Bearer token, 7-day demo sessions |
| Cameras | `GET/POST /cameras`, `POST /cameras/{id}/start\|stop`, `GET /cameras/{id}/snapshot`, `GET /cameras/{id}/stream.mjpeg` | Recording + RTSP sources; auto-start on register |
| Equipment | `GET /equipment*`, `GET /equipment/{id}/available-cameras` | Fleet registry, Safety Zone radius per unit, cabin labels |
| Users (HSE) | `GET/POST /users`, `PUT /users/{id}/password`, `PATCH /users/{id}` | Create/reset/disable operators |
| Assignments | `GET/POST/DELETE /assignments` | One active assignment per unit and per operator |
| Operator | `GET /me/alerts`, `GET /me/assignment`, `GET /me/equipment`, `PUT /me/shift`, `PUT /me/operation`, `POST /me/device` | Shift binding + operation gate |
| Alerts | `GET /alerts`, `GET /events` (SSE), `GET /ws` (WebSocket) | Typed `zoneStatus` + `detectionCounts` |
| Test | `POST /test/alert` | Only when `ENABLE_TEST_ALERTS=1` |

Full contract lives in `migrations/` and `Sources/api/handlers.py`.

---

## Configuration

All keys are optional — the server runs with defaults. Override via `.env` (see `.env.example`) or environment variables.

| Key | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://safety_app:…@localhost:5432/safety_system` | Created by `reset_database.py` |
| `ZONE_RADIUS_M` | `4` | Safety zone confirm radius (meters); clear radius is 6 m |
| `ZONE_RADIUS_M` / `--zone-radius` | `4` | Per-unit override via HSE slider (`equipment/zone.py`) |
| `DETECTION_MODEL_PATH` | `models/best_v8_3class.pt` | Bundled YOLOv8 3-class (excavator/arm/person) |
| `DETECTION_CONF` | `0.40` | Inference confidence threshold |
| `DETECTION_IMGSZ` | `1280` | Inference input size |
| `EVIDENCE_DIR` | system temp | Burst JPEG evidence |
| `APNS_*` | `mock` | Push — mock by default; set `APNS_MODE` + `.p8` for real APNs |
| `ENABLE_TEST_ALERTS` | `0` | Enable `POST /test/alert` |
| `--host` / `--port` | `127.0.0.1:8080` | Use `0.0.0.0` for LAN access |
| `--calib` | — | `GroundCalibrationV2` `.calib.json` for meter-mode zones |
| `--auto-start-cameras` | off | Start all recording cameras on boot |

Model deps (optional, not needed for review):

```sh
.venv/bin/pip install -r requirements-ml.txt
```

---

## Architecture (brief)

```
Sources/core/          Pure domain — Detection, RiskEvaluator (meter 4m/6m or pixel fallback),
                       GroundCalibrationV2 homography, hazard hysteresis (confirm 0.12s / clear 0.75s)
Sources/detection/     YOLOv8 model wrapper (lazy import)
Sources/cameras/       CRUD + per-camera playback thread + MJPEG/RTSP + supervisor
Sources/api/           HTTP handlers + WebSocket
Sources/alerts/        Event bus + history/routing + APNs HTTP/2 JWT (50-min reuse)
Sources/assignments/   Single active assignment per unit/operator
Sources/equipment/     Fleet registry, zone radius, operation status
Sources/infrastructure/database/  Provisioning, connection, migrations, backups
migrations/            Versioned SQL (users, sessions, assignments, alerts, cameras)
models/                Bundled weights (5.3 MiB)
Tools/                 seed_demo.py (sole data factory), reset_database.py, replay_video.py
tests/                 Behaviour + import-linter architecture guard (test_architecture.py)
```

`Sources/core` has no imports from other `Sources` modules (clean-lite contract, enforced by `.importlinter`).

---

## Troubleshooting for reviewers

| Symptom | Fix |
|---|---|
| `Connection refused` from iPhone | Mac and iPhone must be on same Wi-Fi; start server with `--host 0.0.0.0`; use `http://<Mac-IP>:8080/` not `localhost` |
| `invalidURL` on login | Include `http://` and trailing `/`; private IPs only work on Debug/TestFlight builds |
| `No cameras` after seed | Run `Tools/fetch_recordings.py` first, then `reset_database.py --video-dir recordings`; or add RTSP via HSE → Cameras |
| `401` / session expired | Re-seed or re-login; demo sessions last 7 days (`HARNES_SEED_SESSION_DAYS`) |
| `modelLoaded: false` | Expected without `requirements-ml.txt`; demo cameras/alerts still work |
| Camera `error` / `fallback` | RTSP producer not running or file moved; re-add via app or `python -m Sources.cameras.cli list` |
| `fetch_recordings.py` 404 | Private repo — run `gh auth login` first, then re-run; or make repo public |

---

## License

Internal use for HARNES App Review and development. Do not commit `.env`, `.secrets/`, `recordings/`, or `*.p8` keys.
