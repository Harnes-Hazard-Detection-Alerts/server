"""Alert publication and frame-processing integration."""
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from threading import BoundedSemaphore, Lock

from ..api.context import AppContext
from ..api.handlers import Handler
from ..auth.limits import LoginLimits
from ..auth.service import AuthService
from ..config import env_value
from ..core.detection import mark_operators
from ..core.evaluator import RiskEvaluator


def _env_int(name, default):
    """Baca integer dari env (atau .env Server) — default bila kosong/ rusak."""
    raw = env_value(name)
    try:
        return int(raw) if raw not in (None, '') else default
    except (TypeError, ValueError):
        return default


# Alarm management 16 Nov 2026: SATU insiden = SATU bunyi. Push untuk kind
# yang sama pada unit yang sama dijeda PUSH_COOLDOWN_MS (default 120 s);
# eskalasi (person_high_risk) selalu lolos. Flood — lebih dari
# FLOOD_ALERT_LIMIT alert untuk satu unit dalam FLOOD_WINDOW_MS — memperpanjang
# jeda push menjadi FLOOD_PUSH_COOLDOWN_MS. Alert tetap terekam penuh di
# riwayat; yang dijeda hanya bunyi ke perangkat.
PUSH_COOLDOWN_MS = _env_int('HARNES_PUSH_COOLDOWN_MS', 120_000)
FLOOD_WINDOW_MS = 600_000
FLOOD_ALERT_LIMIT = _env_int('HARNES_FLOOD_ALERT_LIMIT', 10)
FLOOD_PUSH_COOLDOWN_MS = _env_int('HARNES_FLOOD_PUSH_COOLDOWN_MS', 300_000)
# Pengingat (alarm management 16 Nov): insiden yang masih berdiri tanpa
# respons operator reminded tiap REMINDER_INTERVAL_MS, maksimal REMINDER_MAX
# kali per insiden. Cara ini menjaga "bahaya masih ada" tanpa banned bunyi
# tiap deteksi.
REMINDER_INTERVAL_MS = _env_int('HARNES_REMINDER_INTERVAL_MS', 300_000)
REMINDER_MAX = _env_int('HARNES_REMINDER_MAX', 3)
REMINDER_CHECK_INTERVAL_S = 30
# Kanal live ke iPhone: event alert untuk key yang sama (unit + kind) di-throttle
# 2 detik — iPhone tidak lagi menerima ratusan event alert yang sama.
ALERT_LIVE_THROTTLE_S = 2.0


class AlertServer(ThreadingHTTPServer):
    """Transport HTTP + pipeline frame; dependensi hidup di AppContext.

    Context menjadi satu-satunya jangkauan lapisan HTTP
    (self.server.context.*) — aturan M5, mengganti kontrak atribut
    tersembunyi. Konstruktor lama (auth/risk/detector positional) tetap
    diterima demi test dan konsumen lama; semuanya dilipat ke AppContext.

    Asumsi thread: process_frame dipanggil dari thread playback (satu per
    kamera sumber) dan/atau thread --video; DETEKSI diserialisasi lock di
    DetectionModel (runtime YOLO tidak thread-safe) — state evaluator tetap
    aman karena dipartisi per source_id. Antrean listener bus diamankan
    lock di EventBus.
    """

    def __init__(self, address, auth=None, risk=None, detector=None, context=None):
        # socketserver may call server_close() if bind fails, so shutdown state
        # must exist before the base constructor opens the listening socket.
        self._push_executor = None
        self._push_executor_lock = Lock()
        self._push_slots = BoundedSemaphore(32)
        self._push_overflow = deque(maxlen=32)
        self._push_overflow_dropped = 0
        self._push_closed = False
        # Gerbang push per unit (alarm management 16 Nov): (equipment, kind)
        # -> epoch-ms push terakhir; volume alert per unit untuk deteksi flood.
        self._push_gate = {}
        self._alert_volume = {}
        self._reminders = {}
        self._reminder_thread = None
        self._push_sent = 0
        self._push_suppressed = 0
        self._push_counters_lock = Lock()
        self._push_summary_at = 0
        # Fast alert 12 Nov: emit worker — persist+publish+push keluar dari
        # jalur thread playback (frame kamera tak pernah menunggu DB/APNs).
        from queue import Queue
        self._emit_queue = Queue()
        self._emit_thread = None
        self._emit_lock = Lock()
        super().__init__(address, Handler)
        if context is not None:
            self.context = context
        else:
            from ..alerts.bus import EventBus
            from ..infrastructure.database.connection import connect
            auth_service = auth or AuthService()
            self.context = AppContext(auth=auth_service,
                                      login_limits=LoginLimits(),
                                      risk=risk or RiskEvaluator(),
                                      detector=detector,
                                      bus=EventBus(),
                                      # Konstruktor legacy test harus tetap memakai
                                      # factory DB yang disuntikkan ke AuthService.
                                      connect=getattr(auth_service, 'connect', connect))
        self.context.cameras.attach(self)

    def process_frame(self, frame, timestamp_ms=None, source_id=None, now_ms=None):
        """Entry point for a video source when frames become available.

        now_ms: jam evaluasi (epoch-ms); None = jam dinding — parameter injeksi
        waktu untuk pengujian deterministik, bukan panggilan produksi."""
        detector = self.context.detector
        if detector is None or not detector.ready:
            statuses = self.context.risk.mark_source_unobserved(source_id, now_ms=now_ms)
            self.publish_statuses(statuses, source_id,
                                  now_ms if now_ms is not None else time.time_ns() // 1_000_000)
            return (), None
        detections = detector.detect(frame)
        # Person di dalam bbox excavator = operator (keputusan 11 Nov): bukan
        # bahaya — ditandai sekali di sini; evaluator, patokan ruler R1, dan
        # logic overlay semuanya membaca tanda yang sama. Box tetap digambar.
        mark_operators(detections)
        alert = self.context.risk.evaluate(detections, timestamp_ms=timestamp_ms,
                                            source_id=source_id, now_ms=now_ms)
        if alert is not None:
            now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
            # Fast alert 12 Nov: emit pindah ke worker thread — persist +
            # publish + push TIDAK PERNAH menahan jalur playback kamera
            # (kejadian 12 Nov: emit inline menggantung frame berikutnya
            # 1.3 s saat 4 kamera 4K jalan bersamaan).
            self._queue_emit(alert, detections, source_id, now)
        else:
            self.publish_statuses(self.context.risk.unit_statuses(source_id), source_id,
                                  now_ms if now_ms is not None else time.time_ns() // 1_000_000,
                                  detections=detections)
        return detections, alert

    def observation_unavailable(self, source_id):
        now = time.time_ns() // 1_000_000
        statuses = self.context.risk.mark_source_unobserved(source_id, now_ms=now)
        # There may be no next frame after source loss. Never drop this final
        # state transition behind the ordinary per-frame status throttle.
        self.publish_statuses(statuses, source_id, now, force=True)

    def publish_statuses(self, statuses, source_id=None, now_ms=None, detections=(), force=False):
        bus = self.context.bus
        if not hasattr(bus, 'publish_typed'):
            return
        now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        counts = self._detection_counts(detections)
        try:
            from ..alerts.bus import EQUIPMENT_STATUS
            for status in statuses if isinstance(statuses, list) else []:
                if not force and not bus._throttle_ok(f"equip:{status['equipmentInstanceId']}",
                                        1.0, now=now / 1000.0):
                    continue
                unit_id = status['equipmentInstanceId']
                try:
                    route = (self.context.store.resolve_binding_operator(unit_id)
                             if self.context.store is not None else None)
                except Exception:
                    route = None
                bus.publish_typed(EQUIPMENT_STATUS, {
                    'equipmentInstanceId': unit_id,
                    'zoneStatus': status['zoneStatus'],
                    'monitoringStatus': status.get('monitoringStatus', 'unknown'),
                    'operationActive': status.get('operationActive', False),
                    'detectionCounts': counts,
                    'lastSeenMs': status['lastSeenMs'],
                }, route_operator_id=route)
        except (AttributeError, TypeError):
            return

    def emit_alert(self, alert, detections=(), source_id=None, now_ms=None):
        """Persist and deliver an already-evaluated alert through every channel.

        The normal caller is ``process_frame``. The gated HSE test endpoint uses
        the same seam so manual danger/safe checks exercise real routing, history,
        WebSocket, and APNs rather than a separate demo path.
        """
        now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        stored = self._persist(alert, now)
        operator_id = self._route_operator(alert)
        self.publish(alert, route_operator_id=operator_id)
        self._publish_typed(alert, stored, detections, source_id, operator_id, now)
        self._deliver(stored, operator_id, now)
        return stored

    def _queue_emit(self, alert, detections, source_id, now_ms):
        """Antrekan emit untuk worker (fast alert 12 Nov) + pulangkan cepat.

        Jalur playback kamera tidak pernah menunggu persist/publish/push —
        kejadian 12 Nov: emit inline menggantung frame berikutnya 1.3 s saat
        empat kamera 4K dievaluasi bersamaan. Urutan antar-frame dijaga
        karena tiap kamera satu thread dan queue diproses FIFO.
        """
        with self._emit_lock:
            if self._emit_thread is None:
                from threading import Thread
                self._emit_thread = Thread(target=self._emit_worker,
                                           name='harnes-alert-emit', daemon=True)
                self._emit_thread.start()
        if not self._emit_queue.empty() and self._emit_queue.qsize() > 100:
            print('[alerts] emit queue >100; pipeline emit lebih lambat dari '
                  'produksi alert', flush=True)
        self._emit_queue.put((alert, detections, source_id, now_ms))

    def _emit_worker(self):
        while True:
            try:
                alert, detections, source_id, now_ms = self._emit_queue.get(timeout=1.0)
            except Exception:
                if self._push_closed:
                    return
                continue
            try:
                self.emit_alert(alert, detections=detections, source_id=source_id,
                                now_ms=now_ms)
            except Exception as error:
                print(f'[alerts] emit worker gagal: {type(error).__name__}',
                      flush=True)

    def _publish_typed(self, alert, stored, detections, source_id, operator_id, now):
        """Dual-emit typed events (api-draft 4.8) selain mac-message legacy."""
        bus = self.context.bus
        if not hasattr(bus, 'publish_typed'):
            return
        from ..alerts.bus import ALERT_CREATED, ALERT_RESOLVED, EQUIPMENT_STATUS
        # alert.created / alert.resolved
        payload = dict(alert)
        if stored:
            payload.update(id=stored['id'], createdAtMs=stored.get('createdAtMs', now))
        kind = alert.get('kind')
        equipment = alert.get('equipmentInstanceId') or ''
        throttle = {'throttle_key': f'alert:{equipment}:{kind}',
                    'min_interval_s': ALERT_LIVE_THROTTLE_S}
        if kind == 'hazard_active_10s':
            payload['hazardActive10sAtMs'] = (stored or {}).get('hazardActive10sAtMs', now)
            bus.publish_typed('alert.hazard_active', payload,
                              route_operator_id=operator_id, **throttle)
            self.publish_statuses(self.context.risk.unit_statuses(source_id), source_id, now,
                                  detections=detections)
            return
        if kind == 'zone_cleared':
            bus.publish_typed(ALERT_RESOLVED, payload, route_operator_id=operator_id,
                              **throttle)
        else:
            bus.publish_typed(ALERT_CREATED, payload, route_operator_id=operator_id,
                              **throttle)
        self.publish_statuses(self.context.risk.unit_statuses(source_id), source_id, now,
                              detections=detections)

    @staticmethod
    def _detection_counts(detections):
        from ..core.detection import CLASS_EXCAVATOR, CLASS_PERSON
        return {
            'person': sum(1 for d in detections if d.get('classId') == CLASS_PERSON),
            'excavator': sum(1 for d in detections if d.get('classId') == CLASS_EXCAVATOR),
        }

    def _persist(self, alert, now_ms):
        # Fail-open: riwayat gagal ditulis tidak boleh menahan broadcast SSE;
        # evaluasi masih hidup, dan endpoint riwayat akan bolong untuk alert
        # ini sampai pemilik data menindaklanjuti peringatan.
        store = getattr(self.context, 'store', None)
        if store is None:
            return None
        try:
            return store.record(alert, now_ms)
        except Exception as error:
            print(f'[alerts] riwayat gagal ditulis (tetap publish): {error}', flush=True)
            return None

    def _route_operator(self, alert):
        """Operator binding-shift utk alert (routing SSE + push).
        Query shift_bindings yang sebelumnya divalidasi terhadap assignment aktif.
        Fail-open: None bila tak ditemukan."""
        equipment = alert.get('equipmentInstanceId')
        store = getattr(self.context, 'store', None)
        if not equipment or store is None:
            return None
        try:
            return store.resolve_binding_operator(equipment)
        except Exception as error:
            print(f'[alerts] routing gagal dibaca (broadcast HSE saja): {error}', flush=True)
            return None

    def _deliver(self, stored, operator_id, now_ms):
        if stored is None or stored.get('milestoneOnly'):
            return
        store = getattr(self.context, 'store', None)
        if store is None:
            return
        # Kanal live (M6 berjalan): 'sent' = ter-antre ke stream operator yang
        # ONLINE saat publish (presence bus); 'failed' = operator tak punya
        # stream — fallback dashboard HSE tetap berjalan (broadcast), audit
        # tetap jujur. Kanal ini perlu operator ter-route oleh binding tervalidasi.
        if operator_id is not None:
            bus = getattr(self.context, 'bus', None)
            online = bus.online_operators() if bus is not None else set()
            status = 'sent' if operator_id in online else 'failed'
            try:
                store.record_deliveries(stored['id'], operator_id, now_ms, status=status)
            except Exception as error:
                print(f'[alerts] delivery audit gagal dicatat: {error}', flush=True)
        # Kanal push (M7): parallel ke live — route lewat binding shift runtime.
        push_op = operator_id or self._route_push_operator(stored)
        # Kebijakan 12 Nov (fast alert): `zone_cleared` TIDAK di-push APNs —
        # 5.147 push clear/60 j era flood adalah riwayat; de-eskalasi cukup
        # lewat event stream (SSE/WS) + notifikasi lokal app operator.
        # Gerbang push 16 Nov: satu bunyi per insiden per unit; alert tetap
        # terekam penuh di riwayat dan channel live.
        push_kinds = {'person_entered_zone', 'person_high_risk'}
        kind = (stored or {}).get('kind')
        equipment = (stored or {}).get('equipmentInstanceId') or ''
        flood = self._note_alert_volume(equipment, now_ms)
        if push_op and kind in push_kinds:
            if self._push_allowed(kind, equipment, now_ms, flood):
                self._push(stored, push_op, now_ms)
            else:
                with self._push_counters_lock:
                    self._push_suppressed += 1
                print(f'[push] bunyi ditahan (jeda per unit) kind={kind} '
                      f'unit={equipment}', flush=True)
        self._maybe_log_push_summary(now_ms)

    def _note_alert_volume(self, equipment, now_ms):
        """Catat volume alert unit; True bila flooding (>limit per 10 menit)."""
        stamps = self._alert_volume.setdefault(equipment, [])
        cutoff = now_ms - FLOOD_WINDOW_MS
        stamps[:] = [stamp for stamp in stamps if stamp >= cutoff]
        stamps.append(now_ms)
        return len(stamps) > FLOOD_ALERT_LIMIT

    def _push_allowed(self, kind, equipment, now_ms, flood=False):
        """Satu push sejenis per unit per jeda; eskalasi high-risk selalu lolos."""
        if kind == 'person_high_risk':
            self._push_gate[(equipment, kind)] = now_ms
            return True
        cooldown = FLOOD_PUSH_COOLDOWN_MS if flood else PUSH_COOLDOWN_MS
        last = self._push_gate.get((equipment, kind))
        if last is not None and now_ms - last < cooldown:
            return False
        self._push_gate[(equipment, kind)] = now_ms
        return True

    def _maybe_log_push_summary(self, now_ms):
        """Ringkasan bunyi tiap menit — KPI alarm (target <=12 push/jam/operator)."""
        if now_ms - self._push_summary_at < 60_000:
            return
        self._push_summary_at = now_ms
        with self._push_counters_lock:
            sent, suppressed = self._push_sent, self._push_suppressed
        print(f'[push] ringkasan 60 dtk: terkirim={sent} ditahan={suppressed}',
              flush=True)

    def _ensure_reminder_thread(self):
        """Thread pengingat hidup lazily (hanya saat ada push pertama)."""
        if self._reminder_thread is not None:
            return
        from threading import Thread
        self._reminder_thread = Thread(target=self._reminder_worker,
                                       name='harnes-alert-reminder', daemon=True)
        self._reminder_thread.start()

    def _reminder_worker(self):
        """Pengecek pengingat berkala — berhenti saat server ditutup."""
        while not self._push_closed:
            time.sleep(REMINDER_CHECK_INTERVAL_S)
            if self._push_closed:
                return
            try:
                self._send_due_reminders()
            except Exception as error:
                print(f'[push] pengingat gagal: {type(error).__name__}', flush=True)

    def _send_due_reminders(self):
        """Pengingat: insiden masih berdiri dan operator belum merespons.

        Maksimal REMINDER_MAX per insiden; berhenti begitu operator merespons
        (kolom operator_response terisi). Bentuk push sama dengan alert biasa —
        payload APNs tidak berubah."""
        store = getattr(self.context, 'store', None)
        if store is None:
            return
        now = time.time_ns() // 1_000_000
        for row in store.active_danger_units():
            equipment = row.get('equipmentInstanceId')
            if not equipment or row.get('operatorResponse'):
                continue
            last_push = self._push_gate.get((equipment, 'person_entered_zone'))
            if last_push is None:
                continue
            # Jarak diukur dari pengingat sebelumnya (bukan dari push masuk),
            # jadi pengingat tidak ikut-PAGE berubah-ubah tiap tick.
            state = self._reminders.setdefault(equipment, {'count': 0,
                                                            'last_ms': last_push})
            if now - state['last_ms'] < REMINDER_INTERVAL_MS:
                continue
            if state['count'] >= REMINDER_MAX:
                continue
            state['count'] += 1
            state['last_ms'] = now
            stored = {'id': row['id'], 'kind': 'person_entered_zone',
                      'message': 'Masih di zona bahaya — belum ada konfirmasi operator.',
                      'equipmentInstanceId': equipment,
                      'personsInZone': row.get('personsInZone')}
            operator_id = self._route_push_operator(stored)
            if operator_id:
                self._push(stored, operator_id, now)

    def _route_push_operator(self, stored):
        """Operator utk push — via binding shift (api-draft 4.6)."""
        equipment = (stored or {}).get('equipmentInstanceId')
        if not equipment:
            return None
        store = getattr(self.context, 'store', None)
        if store is None:
            return None
        try:
            return store.resolve_binding_operator(equipment)
        except Exception:
            return None

    def _push(self, stored, operator_id, now_ms):
        """Queue APNs work so network latency never stalls frame processing."""
        self._ensure_reminder_thread()
        if not self._push_slots.acquire(blocking=False):
            # Jangan menyentuh database dari thread frame. Simpan audit overflow
            # di ring kecil; callback worker akan mengurasnya saat satu slot
            # tersedia. Burst ekstrem tetap dibatasi memori.
            with self._push_executor_lock:
                if len(self._push_overflow) == self._push_overflow.maxlen:
                    self._push_overflow_dropped += 1
                    print('[push] overflow audit penuh; '
                          f'dropped_total={self._push_overflow_dropped}', flush=True)
                    return
                self._push_overflow.append((stored, operator_id, now_ms))
            print('[push] antrean penuh; audit kegagalan ditunda', flush=True)
            return
        with self._push_executor_lock:
            if self._push_closed:
                self._push_slots.release()
                return
            if self._push_executor is None:
                self._push_executor = ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix='appleapp-apns')
            try:
                future = self._push_executor.submit(
                    self._push_sync, stored, operator_id, now_ms)
            except RuntimeError:
                self._push_slots.release()
                return
            future.add_done_callback(self._push_task_done)

    def _push_task_done(self, _future):
        if not _future.cancelled():
            error = _future.exception()
            if error is not None:
                print(f'[push] worker gagal: {type(error).__name__}', flush=True)
        self._push_slots.release()
        with self._push_executor_lock:
            if (self._push_closed or not self._push_overflow
                    or not self._push_slots.acquire(blocking=False)):
                return
            stored, operator_id, now_ms = self._push_overflow.popleft()
            executor = self._push_executor
            if executor is None:
                self._push_slots.release()
                return
            try:
                audit = executor.submit(
                    self._record_push_queue_failure, stored, operator_id, now_ms)
            except RuntimeError:
                self._push_slots.release()
                return
            audit.add_done_callback(self._push_task_done)

    def _record_push_queue_failure(self, stored, operator_id, now_ms):
        store = getattr(self.context, 'store', None)
        if store is None:
            return
        try:
            for device_id, _ in store.get_device_tokens(operator_id):
                store.record_delivery(stored['id'], operator_id, now_ms,
                                      status='failed',
                                      push_device_id=device_id)
        except Exception as error:
            print(f'[push] queue-full audit gagal: {type(error).__name__}',
                  flush=True)

    def _push_sync(self, stored, operator_id, now_ms):
        """Send push on the dedicated worker and record the APNs result."""
        store = getattr(self.context, 'store', None)
        if store is None:
            return
        # Recheck immediately before the network send. A logout, unassignment,
        # or Finish Operation can happen after the alert was queued but before
        # this worker runs; queued work must not outlive those authorization gates.
        equipment = (stored or {}).get('equipmentInstanceId')
        try:
            current_operator = (store.resolve_binding_operator(equipment)
                                if equipment else None)
        except Exception as error:
            print(f'[push] route recheck gagal: {type(error).__name__}', flush=True)
            return
        if str(current_operator) != str(operator_id):
            return
        try:
            tokens = store.get_device_tokens(operator_id)
        except Exception as error:
            print(f'[push] token lookup gagal: {type(error).__name__}', flush=True)
            return
        for device_id, push_token in tokens:
            try:
                result = self._push_sender().send(push_token, stored or {})
                status = ('sent' if result.get('ok') and not result.get('mock')
                          else 'failed')
                if status == 'sent':
                    with self._push_counters_lock:
                        self._push_sent += 1
                store.record_delivery(stored['id'], operator_id, now_ms,
                                      status=status, push_device_id=device_id)
                if result.get('status') == 410:
                    store.remove_device_token(device_id)
                print('[push] result '
                      f'status={result.get("status")} reason={result.get("reason")} '
                      f'apns_id={result.get("apns_id")}', flush=True)
            except Exception as error:
                try:
                    store.record_delivery(stored['id'], operator_id, now_ms,
                                          status='failed',
                                          push_device_id=device_id)
                except Exception:
                    pass
                print(f'[push] gagal: {type(error).__name__}', flush=True)

    def _push_sender(self):
        """Lazy singleton PushSender."""
        from .push import PushSender
        with self._push_executor_lock:
            if not hasattr(self, '_push_instance'):
                self._push_instance = PushSender()
            return self._push_instance

    def server_close(self):
        with self._push_executor_lock:
            self._push_closed = True
            executor = self._push_executor
            self._push_executor = None
            overflow = list(self._push_overflow)
            self._push_overflow.clear()
        if executor is not None:
            # Antrean sudah dibatasi 32 item. Drain secara graceful agar setiap
            # alert yang diterima menghasilkan hasil APNs/audit sebelum client
            # HTTP/2 ditutup.
            executor.shutdown(wait=True, cancel_futures=False)
        for item in overflow:
            self._record_push_queue_failure(*item)
        sender = getattr(self, '_push_instance', None)
        if sender is not None:
            sender.close()
        # Emit worker: beri waktu drain sebelum socket ditutup (worker keluar
        # sendiri saat _push_closed + queue kosong).
        worker = getattr(self, '_emit_thread', None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        reminder = getattr(self, '_reminder_thread', None)
        if reminder is not None and reminder.is_alive():
            reminder.join(timeout=2)
        super().server_close()

    def publish(self, alert, route_operator_id=None):
        self.context.bus.publish(alert, route_operator_id=route_operator_id)
