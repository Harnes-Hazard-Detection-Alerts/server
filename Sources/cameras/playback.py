"""Playback manager kamera — multi-sumber, snapshot frame terbaru (api-draft 4.3).

Satu kamera aktif = satu thread playback (pacing realtime memakai jam container
seperti video_input._play), memanggil AlertServer.process_frame per frame dan
menyimpan frame terakhir untuk snapshot JPEG (raw/annotated) dengan ETag per
frame. Transisi status (online/offline/error) dipersisten ke tabel cameras
via service.set_status.

Dependensi sengaja: reader di-reuse dari video_input (`RecordingReader`) —
adapter pembaca file, bukan logika fitur; status DB via connection_factory.
Kamera TIDAK bergantung ke detector/risk/alerts: panggilan pipeline lewat
callback `process_frame` milik AlertServer (disuntik saat attach).
"""
import os
import time
from collections import deque
from threading import Event, Lock, Thread
from uuid import uuid4

from ..cameras import service
from ..cameras.evidence import EvidenceRecorder
from ..equipment.operation import OperationStatus
from ..video_input.reader import RecordingReader

# ≈ 10 detik bufer pada sampling 3 fps; ring-buffer frame per kamera disimpan
# di sini dan dibekukan jadi burst saat alert terbit.
RING_BUFFER_FRAMES = 30

# Gate model (owner 13 Nov 2026): deteksi hanya jalan untuk kamera dengan unit
# yang operatornya sedang operating. TTL cache daftar kamera "hot": Start/Stop
# Operating dari app terasa maksimal segini detik kemudian.
MODEL_GATE_TTL_S = 5.0

# Kamera dingin (tak ada unit operating) tetap menarik frame agar koneksi RTSP
# hidup, tapi snapshot/ring-buffer hanya disegar pada laju ini — bukan tiap
# frame. 8 fps → 1 fps = ~8x lebih sedikit copy/resize/append per detik, dan
# detector memang sudah dilewati total saat dingin. 0 = bekukan frame terakhir
# (snapshot basi, stream.mjpeg menutup via fresh()).
IDLE_KEEPALIVE_FPS = float(os.environ.get('HARNES_IDLE_KEEPALIVE_FPS') or '1.0')


def _idle_keepalive_stride(effective_fps):
    """Co-state frame ke-N yang disimpan saat kamera dingin.

    Return 0 = jangan simpan sama sekali (beku); selain itu simpan tiap
    frame ke-N (count % stride == 0) agar snapshot/ring segar pada
    IDLE_KEEPALIVE_FPS."""
    if IDLE_KEEPALIVE_FPS <= 0:
        return 0
    return max(1, round((effective_fps or 1) / IDLE_KEEPALIVE_FPS))


# Jeda tick supervisor dan jendela tenang restart RTSP (anti-spam saat
# produser mati: fallback 3x → tunggu jendela → coba lagi — self-heal).
SUPERVISOR_INTERVAL_S = float(os.environ.get(
    'HARNES_SUPERVISOR_INTERVAL_S') or '5')
RTSP_RETRY_S = float(os.environ.get('HARNES_CAMERA_RETRY_S') or '30')
# Batas "frame beku" sebelum restart dianggap perlu (reader macet tanpa data).
SUPERVISOR_STALE_MS = 30_000


class CameraManager:
    """Registry runtime: thread per kamera aktif + frame/deteksi terakhir."""

    def __init__(self, connection_factory=None):
        self.connection_factory = connection_factory
        self._server = None
        self._threads = {}      # camera_id -> (Thread, Event stop)
        self._frames = {}       # camera_id -> (frame, counter)
        self._detections = {}   # camera_id -> tuple deteksi frame terakhir
        self._ring = {}         # camera_id -> deque(maxlen) berisi (timestamp_ms, frame)
        self._last_frame_ms = {}  # camera_id -> epoch ms frame terakhir ((deteksi stale)
        self._ruler = {}        # camera_id -> deque px_per_m (patokan person R1)
        self._source_size = {}  # camera_id -> (w, h) resolusi ASLI sebelum downscaled:
                                # kalibrasi homografi (calibrations/) dianotasi di
                                # resolusi sumber (4K) — geometri zona dihitung di
                                # ruang itu lalu diturunkan ke ruang frame delivers.
        self._px_per_m = {}     # camera_id -> median px_per_m hasil patokan
        self._evidence_recorder = None  # EvidenceRecorder lazy; None = nonaktif
        self._evidence_resolved = False  # resolve sekali (hindari ulang warning)
        self._hot_cache = None   # (monotonic, set(camera_id) | None) — gate model
        # Kernel supervisor (keputusan owner 14 Nov 2026): intent desired_on
        # (migrasi 017) dijamin oleh reconciliation loop — "start" berubah
        # dari event sekali-tekan menjadi kondisi yang terus dipertahankan.
        self._retry_at = {}        # camera_id -> epoch-ms jendela tenang RTSP
        self._sup_thread = None
        self._sup_stop = Event()
        # Seam identitas render (14 Nov): resolver kamera→unit ter-DAFTAR
        # (dipasang main.py dari CameraIdentityCatalog) — overlay radius/elips
        # /garis jarak ikut slider Safety Zone unit hasil relink juga.
        self.unit_identity_for = None
        self._lock = Lock()

    # -- wiring ---------------------------------------------------------------
    def attach(self, server):
        """Ambil referensi AlertServer (sumber process_frame + connect). Idempotent."""
        self._server = server
        if self.connection_factory is None:
            context = getattr(server, 'context', None)
            connect = getattr(context, 'connect', None)
            if connect is not None:
                self.connection_factory = connect

    def autostart(self):
        """Start semua kamera terdaftar (flag --auto-start-cameras)."""
        if self.connection_factory is None:
            return []
        started = []
        for camera in service.list_cameras(connection_factory=self.connection_factory):
            self.start(camera)
            started.append(camera['id'])
        if started:
            print(f'[cameras] auto-start {len(started)} kamera (recording + rtsp)', flush=True)
        return started

    # -- playback -------------------------------------------------------------
    def _open_reader(self, camera):
        """Pembaca frame per tipe kamera (api-draft 4.3): recording = file loop;
        rtsp = stream live CCTV (RTSPReader; reconnect maksimal 3x open gagal
        beruntun lalu berhenti → fallback via status error)."""
        from ..video_input.reader import RTSPReader, RecordingReader
        if camera['type'] == 'rtsp':
            def on_state(state):
                cid = camera['id']
                if state == 'stale':
                    self._set_status(cid, 'error',
                                    'stream hilang: produser RTSP tidak merespons')
                else:
                    self._set_status(cid, 'online', 'stream hidup kembali')
            return RTSPReader(camera['source'], camera.get('playbackFps'),
                              on_state=on_state)
        return RecordingReader(camera['source'], camera.get('playbackFps'))

    def start(self, camera):
        """Nyalakan playback kamera (idempoten); rtsp → RTSPReader live."""
        camera_id = camera['id']
        with self._lock:
            running = self._threads.get(camera_id)
            if running and running[0].is_alive():
                return False
            stop = Event()
            thread = Thread(target=self._play, daemon=True,
                            name=f'camera-{camera_id[:8]}',
                            args=(dict(camera), stop))
            self._threads[camera_id] = (thread, stop)
        self._set_status(camera_id, 'online', 'playback dimulai')
        self._mark_desired(camera_id, True)
        thread.start()
        return True

    def stop(self, camera_id):
        """Hentikan playback (idempotent); status offline bila sebelumnya hidup.
        Stop = INTENT off (017): supervisor tidak akan membangkitkan kembali."""
        with self._lock:
            pair = self._threads.pop(camera_id, None)
        if pair is None:
            self._mark_desired(camera_id, False)  # idempoten: stop di kondisi mati
            return False
        thread, stop_event = pair
        stop_event.set()
        thread.join(timeout=10)
        self._set_status(camera_id, 'offline', None)
        self._mark_desired(camera_id, False)
        return True

    def is_running(self, camera_id):
        with self._lock:
            pair = self._threads.get(camera_id)
            return bool(pair and pair[0].is_alive())

    # -- supervisor: intent desired_on → runtime (keputusan owner 14 Nov) ----
    def _mark_desired(self, camera_id, desired):
        """Tulis intent (fail-open: supervisor hanya baca saat tick berikut)."""
        if self.connection_factory is None:
            return
        try:
            service.set_desired(camera_id, desired,
                                connection_factory=self.connection_factory)
        except Exception as error:
            print(f'[supervisor] gagal menulis intent {camera_id}: {error}',
                  flush=True)

    def start_supervisor(self):
        """Nyalakan reconciliation loop (idempoten)."""
        if self._sup_thread is not None and self._sup_thread.is_alive():
            return
        self._sup_stop.clear()
        self._sup_thread = Thread(target=self._supervise, daemon=True,
                                  name='camera-supervisor')
        self._sup_thread.start()

    def stop_supervisor(self):
        self._sup_stop.set()
        thread, self._sup_thread = self._sup_thread, None
        if thread is not None:
            thread.join(timeout=SUPERVISOR_INTERVAL_S + 2)

    def _supervise(self):
        while not self._sup_stop.is_set():
            try:
                self.reconcile_once()
            except Exception as error:  # satu tick gagal ≠ supervisor mati
                print(f'[supervisor] tick gagal ({type(error).__name__}): {error}',
                      flush=True)
            self._sup_stop.wait(SUPERVISOR_INTERVAL_S)

    def reconcile_once(self):
        """Satu tick jaminan intent→runtime.

        desired_off + jalan → stop (intent HSE dihormati); desired_on +
        belum jalan → start; RTSP fallback-error → tunggu jendela retry
        (anti-spam, self-heal saat produser hidup lagi); RTSP jalan tapi
        frame beku → restart. Status error/cadence diingat per kamera.
        """
        if self.connection_factory is None:
            return
        try:
            cameras = service.list_cameras(connection_factory=self.connection_factory)
        except Exception as error:
            print(f'[supervisor] daftar kamera gagal ({type(error).__name__}) '
                  '— tick dilewati', flush=True)
            return
        now_ms = time.time_ns() // 1_000_000
        for camera in cameras:
            camera_id = camera['id']
            running = self.is_running(camera_id)
            if not camera.get('desiredOn'):
                if running:
                    print(f'[supervisor] {camera["name"]}: intent off — '
                          'playback dihentikan', flush=True)
                    self.stop(camera_id)
                continue
            if running:
                if (camera['type'] == 'rtsp'
                        and not self.fresh(camera_id, SUPERVISOR_STALE_MS)
                        and now_ms >= self._retry_at.get(camera_id, 0)):
                    print(f'[supervisor] {camera["name"]}: frame beku — restart',
                          flush=True)
                    self._retry_at[camera_id] = now_ms + int(RTSP_RETRY_S * 1000)
                    self.stop(camera_id)
                    self.start(camera)
                continue
            # desired, belum jalan
            if (camera['type'] == 'rtsp' and camera['status'] == 'error'
                    and now_ms < self._retry_at.get(camera_id, 0)):
                continue  # jendela tenang — produser RTSP mungkin belum siap
            if camera['type'] == 'rtsp':
                self._retry_at[camera_id] = now_ms + int(RTSP_RETRY_S * 1000)
            print(f'[supervisor] {camera["name"]}: desired — playback dinyalakan',
                  flush=True)
            self.start(camera)


    def _hot_cameras(self):
        """Kamera yang punya unit dengan operator sedang operating (gate model).

        Konsisten dengan OperationStatus (assignment aktif + sesi hidup −
        binding NULL eksplisit/Stop). Kamera di luar set tetap menarik frame
        (RTSP harus hidup) dan snapshot/ring disegar pada IDLE_KEEPALIVE_FPS,
        tapi detector TIDAK jalan — hemat MPS saat tak ada unit yang
        difungsikan. Cache TTL MODEL_GATE_TTL_S;
        kegagalan baca DB = fail-OPEN (None → semua kamera diproses) karena
        proteksi tidak boleh hilang karena kegagalan akses pendukung."""
        now = time.monotonic()
        cache = self._hot_cache
        if cache is not None and now - cache[0] < MODEL_GATE_TTL_S:
            return cache[1]
        hot = None
        if self.connection_factory is not None:
            try:
                status = OperationStatus(self.connection_factory)
                status.reload()
                active = status.active_ids()
                if active:
                    with self.connection_factory() as connection:
                        rows = connection.execute(
                            'SELECT equipment_instance_id, camera_id '
                            'FROM equipment_cameras').fetchall()
                    hot = {str(camera_id) for equipment, camera_id in rows
                           if equipment in active}
                else:
                    hot = set()
            except Exception as error:
                print(f'[cameras] gate operating gagal dibaca '
                      f'({type(error).__name__}) — model jalan fail-open '
                      f'untuk semua kamera streaming', flush=True)
                hot = None
        self._hot_cache = (now, hot)
        return hot

    def _play(self, camera, stop_event):
        """Satu putaran playback; loop mengulang dari awal (epoch baru per putaran)."""
        camera_id = camera['id']
        try:
            while not stop_event.is_set():
                reader = self._open_reader(camera)
                # Anchor epoch: timestamp sumber (ms) → Unix-epoch (api-draft 2),
                # sama untuk recording maupun live RTSP (anchor = jam buka stream).
                epoch0 = time.time_ns() // 1_000_000
                started = time.perf_counter()
                count = 0
                det_total = 0.0
                det_worst = 0.0
                was_hot = None
                try:
                    for index, ts_ms, frame in reader.frames():
                        if stop_event.is_set():
                            break
                        timestamp_ms = epoch0 + ts_ms
                        # Gate model (owner 13 Nov): kamera tanpa unit operating
                        # dilewati detectornya. Frame tetap ditarik (RTSP harus
                        # hidup) tapi snapshot/ring hanya disegar pada
                        # IDLE_KEEPALIVE_FPS — hemat copy/resize CPU per detik.
                        hot_cameras = self._hot_cameras()
                        hot = hot_cameras is None or camera_id in hot_cameras
                        if hot:
                            t_det = time.perf_counter()
                            detections, _alert = self._process(camera_id, frame,
                                                              timestamp_ms=timestamp_ms)
                            det_ms = (time.perf_counter() - t_det) * 1000
                            det_total += det_ms
                            det_worst = max(det_worst, det_ms)
                            self._store_frame(camera_id, frame, detections,
                                              timestamp_ms=timestamp_ms)
                        else:
                            detections = ()
                            det_ms = 0.0
                            stride = _idle_keepalive_stride(reader.effective_fps)
                            if stride and count % stride == 0:
                                self._store_frame(camera_id, frame, detections,
                                                  timestamp_ms=timestamp_ms)
                        if hot != was_hot:
                            state = ('hidup — ada unit operating, deteksi jalan'
                                     if hot else
                                     'idle — model dilewati, snapshot '
                                     f'{IDLE_KEEPALIVE_FPS:g} fps '
                                     '(tak ada unit operating)')
                            print(f'[cameras] {camera["name"]}: model {state}', flush=True)
                            was_hot = hot
                        delay = ((count + 1) / (reader.effective_fps or 1)
                                 - (time.perf_counter() - started))
                        if delay > 0 and not stop_event.wait(delay):
                            pass
                        count += 1
                        if count % 100 == 0:
                            print(f'[cameras] {camera["name"]}: fps={reader.effective_fps:.1f} '
                                  f'det rata2={det_total / count:.1f}ms worst={det_worst:.0f}ms '
                                  f'({count} frame)', flush=True)
                finally:
                    reader.release()
                # Reader berakhir tanpa stop: RTSP = budget reconnect habis di
                # RTSPReader (produser mati, fallback) → jangan re-open di sini;
                # recording: ulang hanya bila loop; tanpa loop → offline, frame
                # akhir tetap tersedia untuk snapshot.
                if (stop_event.is_set() or camera['type'] == 'rtsp'
                        or (camera['type'] == 'recording'
                            and not camera.get('loop'))):
                    break
        except Exception as error:  # kamera bermasalah tidak boleh menjatuhkan server
            if not stop_event.is_set():
                with self._lock:
                    self._threads.pop(camera_id, None)
                self._set_status(camera_id, 'error', f'playback terhenti: {type(error).__name__}')
                print(f'[cameras] {camera["name"]}: {type(error).__name__}: {error}', flush=True)
            return
        if stop_event.is_set():
            return
        # Reader selesai tanpa stop. Recording tanpa loop → offline; frame
        # terakhir tetap tersedia untuk snapshot. RTSP → budget reconnect habis
        # (produser mati, scan sudah berhenti) → error + healthNote fallback;
        # pemulihan lewat start ulang atau restart server.
        with self._lock:
            self._threads.pop(camera_id, None)
        if camera['type'] == 'rtsp':
            self._set_status(camera_id, 'error',
                             'stream RTSP tidak ditemukan — scan dihentikan (fallback)')
            print(f'[cameras] {camera["name"]}: stream RTSP tidak ditemukan '
                  'setelah 3x percobaan — fallback', flush=True)
        else:
            # Video habis tanpa loop = konsumsi selesai (satu putaran):
            # intent ikut off — supervisor tidak boleh memutar ulang file
            # yang memang tidak dimaksudkan berulang (loop=false).
            self._mark_desired(camera_id, False)
            self._set_status(camera_id, 'offline', 'end_of_recording')

    def _process(self, camera_id, frame, timestamp_ms):
        server = self._server
        if server is None:
            return (), None
        detections, alert = server.process_frame(frame, timestamp_ms=timestamp_ms,
                                                 source_id=camera_id)
        if alert is not None:
            # Alert evaluator belum membawa id DB saat ini; identifikasi fallback
            # uuid4 bila id tidak tersedia (api-draft 6 tetap menulis burst).
            alert_id = (alert.get('id') if isinstance(alert, dict) else None)
            self.evidence_burst(camera_id, alert_id or uuid4().hex)
        return detections, alert

    # -- snapshot ---------------------------------------------------------------
    def snapshot(self, camera_id, annotated=True):
        """(jpeg_bytes, etag) frame terbaru, atau None bila belum ada frame
        (handler → 503 camera_offline sesuai api-draft 4.3)."""
        with self._lock:
            captured = self._frames.get(camera_id)
            detections = self._detections.get(camera_id, ())
        if captured is None:
            return None
        frame, counter = captured
        body = self._encode(camera_id, frame, detections if annotated else ())
        etag = f'"{counter}-{"a" if annotated else "r"}"'
        return body, etag

    def stream_frames(self, camera_id, annotated=True):
        """JPEG bytes tiap frame counter berubah — MJPEG feed (api-draft 4.3).

        Frame pertama langsung terkirim saat stream dibuka; selanjutnya hanya
        frame baru. Laju mengikuti sampling playback (fps kamera). Generator
        berhenti begitu socket klien tertutup (pemanggil melempar BrokenPipe).
        """
        last = None
        while True:
            snap = self.snapshot(camera_id, annotated=annotated)
            if snap is None:
                time.sleep(0.25)
                continue
            if not self.fresh(camera_id):
                # Frame beku (produser mati/kamera berhenti): akhiri stream —
                # frame terakhir jangan dikira sebagai feed yang masih hidup.
                return
            body, etag = snap
            counter = int(etag.strip('"').split('-')[0])
            if counter != last:
                last = counter
                yield body
            else:
                time.sleep(0.25)

    def _encode(self, camera_id, frame, detections):
        import cv2  # lazy: bagian requirements-ml; hanya dipanggil saat snapshot
        image = frame if not detections else frame.copy()
        color = {0: (0, 160, 255), 1: (200, 120, 255), 2: (60, 200, 60)}  # BGR
        thickness = max(2, image.shape[1] // 900)
        for detection in detections:
            x1, y1, x2, y2 = detection['box']
            cv2.rectangle(image, (x1, y1), (x2, y2),
                          color.get(detection.get('classId'), (255, 255, 255)),
                          thickness)
        if detections:
            self._draw_safety_overlay(camera_id, image, detections, thickness)
        # Render delivery (snapshot.jpg + stream.mjpeg): anotasikan dulu di
        # ruang pixel frame TERSIMPAN, baru downscale gambar utuh — kotak,
        # garis jarak, dan label ikut terskala otomatis. Deteksi tetap
        # jalan di frame ASLI (akurasi bbox/jarak tak tersentuh).
        #
        # Serve 720p q85 = ~166 KB/frame; feed MJPEG ke app jadi 5-9 Mbps
        # sehingga viewing di WiFi tersendat (CCTV 2.4GHz, Apple 15 Nov).
        # HARNES_SERVE_MAX_WIDTH/HARNES_SERVE_JPEG_QUALITY untuk tuning;
        # 0 = biarkan ukuran/kuualitas aslinya.
        serve_width = int(os.environ.get('HARNES_SERVE_MAX_WIDTH') or '960')
        if serve_width > 0 and image.shape[1] > serve_width:
            scale = serve_width / image.shape[1]
            image = cv2.resize(image, (serve_width,
                                       max(1, round(image.shape[0] * scale))),
                               interpolation=cv2.INTER_AREA)
        quality = int(os.environ.get('HARNES_SERVE_JPEG_QUALITY') or '70')
        if quality <= 0:
            quality = 85
        ok, buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            raise service.CameraError('encode_failed')
        return buffer.tobytes()

    # -- overlay jarak (keputusan owner 12 Nov: tanpa cincin zona) --
    #
    # Render hanya menggambar kotak + titik kaki (gaya v4) dan garis jarak
    # meter excavator↔person terdekat + labelnya. Cincin/zona (elips homografi
    # maupun dashed fallback) dihapus dari render — radius slider HSE tetap
    # jadi ambang alert evaluator dan menentukan warna garis (merah ≤ radius).
    # Jarak: kalibrasi per kamera bila ada (ruang pixel sumber), tanpa calib
    # memakai patokan ruler px/m R1.

    GREEN, YELLOW, RED, WHITE = (80, 220, 80), (0, 215, 255), (60, 60, 255), (255, 255, 255)

    # Kalibrasi homografi per kamera (Server/calibrations/{Nama}.json) —
    # disuntik composition root (main.py): {camera_id: GroundCalibrationV2}.
    # Kamera tanpa calib → fallback ruler px/m / dashed rect.
    ground_calibrations = {}

    @staticmethod
    def _dashed_line(image, start, end, color, thickness, gap=8):
        import cv2
        import math
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        if length < 1:
            return
        steps = max(1, int(length // (gap * 2)))
        for step in range(steps + 1):
            begin = (round(start[0] + (end[0] - start[0]) * step / steps),
                     round(start[1] + (end[1] - start[1]) * step / steps))
            stop = (round(start[0] + (end[0] - start[0]) * (step + 0.5) / steps),
                    round(start[1] + (end[1] - start[1]) * (step + 0.5) / steps))
            cv2.line(image, begin, stop, color, thickness, cv2.LINE_AA)

    @classmethod
    def _dashed_rect(cls, image, box, color, thickness, padding=1.25):
        x1, y1, x2, y2 = box
        width, height = x2 - x1, y2 - y1
        cx, cy = (x1 + x2) // 2, y2
        half_w, half_h = int(width * padding / 2), int(height * padding / 2)
        corners = ((cx - half_w, cy - half_h), (cx + half_w, cy + half_h))
        cls._dashed_line(image, (corners[0][0], corners[0][1]), (corners[1][0], corners[0][1]), color, thickness)
        cls._dashed_line(image, (corners[1][0], corners[0][1]), (corners[1][0], corners[1][1]), color, thickness)
        cls._dashed_line(image, (corners[1][0], corners[1][1]), (corners[0][0], corners[1][1]), color, thickness)
        cls._dashed_line(image, (corners[0][0], corners[1][1]), (corners[0][0], corners[0][1]), color, thickness)

    # Radius zona per kamera (api-draft 4.4a): ikut setelan HSE dari app —
    # ZoneSettings cache (tabel equipment_zone_settings, evaluator-safe).
    # Identitas lewat SEAM: kamera relink memetakan ke unit ter-DAFTAR
    # (slider di app menulis setelan unit itu); kamera tanpa link →
    # fallback evaluator, perilaku lama.
    def _unit_identity(self, camera_id):
        if self.unit_identity_for is not None:
            resolved = self.unit_identity_for(camera_id)
            if resolved:
                return resolved
        return f'{str(camera_id)[:8]}-eq-1'

    def _zone_radius_m(self, camera_id):
        from ..core.evaluator import DEFAULT_ZONE_RADIUS_M
        settings = getattr(getattr(self._server, 'context', None), 'zone_settings', None)
        if settings is not None:
            # Identitas fallback evaluator: 8-char id kamera + '-eq-1' —
            # sama seperti seeding/IOS unit (source_id playback = id kamera);
            # kamera relink diselesaikan _unit_identity di atas.
            identity = self._unit_identity(camera_id)
            value = settings.radius_for(identity)
            if value is not None:
                return float(value)
        return DEFAULT_ZONE_RADIUS_M

    def _draw_safety_overlay(self, camera_id, image, detections, thickness):
        """Overlay jarak — tanpa cincin zona (keputusan owner 12 Nov).

        Yang digambar: kotak deteksi + titik kaki (v4) dan SATU garis
        jarak meter excavator↔person terdekat dengan label "x.x m".
        Cincin/zona (fill homografi, outline, boundary clear, fallback
        dashed) dihapus sepenuhnya dari render — radius slider tetap
        hidup di evaluator (ambang alert) dan menentukan warna garis
        (merah ≤ radius, kuning di luar).

        Jarak meter: kalibrasi homografi per kamera bila ada (geometri
        dihitung di ruang pixel SUMBER — box deliver sudah di-scale);
        tanpa calib memakai patokan ruler px/m R1 (aproksimasi lokal
        pada kedalaman person terdekat). Person bertanda operator
        (di dalam bbox excavator) tak dinilai — box-nya tetap digambar.
    """
        import cv2
        import math
        from ..core.detection import CLASS_EXCAVATOR, CLASS_PERSON
        from ..core.zones import excavator_foot, ground_point

        excavators = [d for d in detections if d.get('classId') == CLASS_EXCAVATOR]
        persons = [d for d in detections if d.get('classId') == CLASS_PERSON]
        if not excavators:
            return
        workers = [d for d in persons if not d.get('operator')]
        calib = (self.ground_calibrations or {}).get(camera_id)
        px_per_m = self._px_per_m.get(camera_id)
        radius_m = self._zone_radius_m(camera_id)
        height, width = image.shape[:2]
        source_w, _ = self._source_size.get(camera_id, (width, height))
        geo_scale = source_w / float(width)  # ruang anotasi calib / ruang gambar

        # 1) Kotak + titik kaki semua deteksi (gaya v4: person hijau,
        #    excavator oranye, kaki titik merah 5px).
        for detection in detections:
            if detection.get('classId') not in (CLASS_EXCAVATOR, CLASS_PERSON):
                continue
            x1, y1, x2, y2 = [int(v) for v in detection['box']]
            color = (60, 200, 60) if detection.get('classId') == CLASS_PERSON else (0, 160, 255)
            cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
            foot = (excavator_foot(detection) if detection.get('classId') == CLASS_EXCAVATOR
                    else ground_point(detection))
            cv2.circle(image, (int(foot[0]), int(foot[1])), 5, (0, 0, 255), -1)

        # 2) Garis jarak excavator↔person (pejalan) terdekat + label.
        best = None  # (dist_m, ex_foot, worker_foot)
        for excavator in excavators:
            ex_foot = excavator_foot(excavator)
            for worker in workers:
                w_foot = ground_point(worker)
                if calib is not None:
                    meters = calib.to_meter(w_foot[0] * geo_scale, w_foot[1] * geo_scale)
                    center_m = calib.to_meter(ex_foot[0] * geo_scale, ex_foot[1] * geo_scale)
                    if meters is None or center_m is None:
                        continue
                    dist = math.dist(meters, center_m)
                elif px_per_m:
                    contact = (min(max(w_foot[0], excavator['box'][0]), excavator['box'][2]),
                               min(max(w_foot[1], excavator['box'][1]), excavator['box'][3]))
                    dist = math.hypot(w_foot[0] - contact[0], w_foot[1] - contact[1]) / px_per_m
                else:
                    continue  # belum ada metrik jarak (ruler belum siap, tanpa calib)
                if best is None or dist < best[0]:
                    best = (dist, ex_foot, w_foot)
        if best is not None:
            dist, ex_foot, w_foot = best
            line_color = (0, 0, 255) if dist <= radius_m else (255, 255, 0)
            x1, y1 = int(ex_foot[0]), int(ex_foot[1])
            x2, y2 = int(w_foot[0]), int(w_foot[1])
            cv2.line(image, (x1, y1), (x2, y2), line_color, 2, cv2.LINE_AA)
            mid = ((x1 + x2) // 2, min((y1 + y2) // 2 - 10, height - 12))
            cv2.putText(image, f"{dist:.1f} m", mid, cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(image, f"{dist:.1f} m", mid, cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (255, 255, 255), 2, cv2.LINE_AA)

    def _store_frame(self, camera_id, frame, detections, timestamp_ms=None):
        with self._lock:
            previous = self._frames.get(camera_id)
            counter = (previous[1] + 1) if previous else 1
            # Frame dikopi: cv2.VideoCapture menimpa buffer ndarray yang sama
            # antar cap.read() — tanpa copy, snapshot & ring-buffer membaca
            # buffer yang tertimpa. Satu copy dirujuk keduanya (read-only).
            #
            # Downscale delivery-only (api-draft 4.3): frame TERSIMPAN (ring,
            # snapshot, stream, evidence) dibatasi HARNES_FRAME_MAX_WIDTH
            # (default 1280) supaya CCTV 4K tidak membengkakkan RAM ring
            # (240MB → ~50MB/kamera) dan bandwidth feed; DETEKSI tetap pada
            # frame ASLI (akurasi bbox/jarak) — hanya hasil render yang
            # mengecil: box di-scale sekali di sini, semua konsumen konsisten.
            copy = frame.copy()
            detections = list(detections or ())
            # Ukuran sumber ASLI dicatat sekali per kamera: kalibrasi
            # homografi per kamera (calibrations/, anotasi 4K native) valid di
            # ruang pixel ini — overlay menghitung zone geometry di ruang asli
            # lalu menurunkannya ke ruang frame deliver (lihat _draw overlay).
            if camera_id not in self._source_size:
                self._source_size[camera_id] = (frame.shape[1], frame.shape[0])
            # Patokan person R1 (sebelum downscale — box masih resolusi asli):
            # tinggi badan utuh ~1.7 m → piksel/meter lokal; rolling median
            # per kamera menghaluskan variasi kedalaman orang bolak-balik.
            ruler = self._ruler.setdefault(camera_id, deque(maxlen=30))
            for detection in detections:
                box = detection.get('box')
                # Operator (person di dalam bbox excavator) BUKAN patokan:
                # bbox-nya terpotong kabin → tinggi menyesatkan ruler px/m.
                if (detection.get('classId') == 2 and not detection.get('operator')
                        and box and box[3] > box[1] + 10):
                    ruler.append((box[3] - box[1]) / 1.7)
            if len(ruler) >= 5:
                ordered = sorted(ruler)
                self._px_per_m[camera_id] = ordered[len(ordered) // 2]
            max_width = int(os.environ.get('HARNES_FRAME_MAX_WIDTH') or '1280')
            if max_width > 0 and copy.shape[1] > max_width:
                import cv2  # lazy: bagian requirements-ml
                scale = max_width / copy.shape[1]
                copy = cv2.resize(copy, (max_width,
                                         max(1, round(copy.shape[0] * scale))),
                                  interpolation=cv2.INTER_AREA)
                detections = [
                    dict(detection, box=tuple(round(value * scale)
                                              for value in detection['box']))
                    for detection in detections
                ]
            self._frames[camera_id] = (copy, counter)
            self._detections[camera_id] = detections
            self._last_frame_ms[camera_id] = timestamp_ms or int(time.time() * 1000)
            ring = self._ring.setdefault(camera_id, deque(maxlen=RING_BUFFER_FRAMES))
            ring.append((timestamp_ms, copy))

    def fresh(self, camera_id, max_age_ms=10_000):
        """True bila frame terbaru masih hangat — pipeline kamera ini hidup.
        Dipakai stream.mjpeg menutup feed bila produser/camera mati supaya
        klien (app) tidak menerima frame beku yang tampak seperti live."""
        with self._lock:
            stamp = self._last_frame_ms.get(camera_id)
        if stamp is None:
            return False
        return int(time.time() * 1000) - stamp <= max_age_ms

    def evidence_burst(self, camera_id, alert_id):
        """Bekukan ring-buffer kamera dan tulis burst JPEG lewat EvidenceRecorder.

        Return dict hasil recorder (path/file_count/duration_span_ms) atau
        None bila evidence nonaktif, ring kosong, atau penulisan gagal (fail-open).
        """
        recorder = self._evidence()
        if recorder is None:
            return None
        with self._lock:
            ring = self._ring.get(camera_id)
            frames = list(ring) if ring else None
        if not frames:
            return None
        try:
            return recorder.record(camera_id, alert_id, frames, {})
        except Exception as error:
            print(f'[cameras] evidence burst gagal {camera_id}/{alert_id}: {error}',
                  flush=True)
            return None

    def _evidence(self):
        """Lazy singleton EvidenceRecorder; sekali resolve (fail-open + warning)."""
        if not self._evidence_resolved:
            self._evidence_resolved = True
            recorder = EvidenceRecorder()
            if recorder.enabled:
                self._evidence_recorder = recorder
            else:
                print('[cameras] evidence nonaktif: EVIDENCE_DIR tidak di-set '
                      'dan direktori default belum ada — skip', flush=True)
        return self._evidence_recorder

    def forget(self, camera_id):
        """Buang frame & deteksi tersimpan (dipanggil saat kamera dihapus)."""
        with self._lock:
            self._frames.pop(camera_id, None)
            self._detections.pop(camera_id, None)
            self._ring.pop(camera_id, None)

    def _set_status(self, camera_id, status, health_note):
        if self._server is not None:
            self._server.observation_unavailable(camera_id)
        if self.connection_factory is None:
            return
        try:
            service.set_status(camera_id, status, time.time_ns() // 1_000_000,
                               health_note=health_note,
                               connection_factory=self.connection_factory)
        except Exception as error:
            print(f'[cameras] gagal simpan status {camera_id}: {error}', flush=True)
