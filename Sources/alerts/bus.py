"""Bus publikasi alert — implementasi port Publisher (core/ports).

Dipindah dari AlertServer (M5): penomoran urut + fan-out ke antrean
listener SSE diamankan satu lock; satu listener = satu koneksi stream.
M6: listener diberi audiens — hse menerima semua; operator HANYA event
equipment yang binding shift-nya sudah divalidasi assignment (api-draft 4.8). Bus juga menjadi registry
kehadiran operator (presence SSE): device 'online' = operator punya stream hidup.
"""
import json
import time
from queue import Queue
from threading import Lock

# Nama-nama event typed (api-draft 4.8 §4.8) — kanal tambahan di samping
# `mac-message` legacy. Konstanta module-level; payload tiap tipe tercatat.
EQUIPMENT_STATUS = "equipment.status"  # {equipmentInstanceId, zoneStatus, detectionCounts, lastSeenMs}
CAMERA_STATUS = "camera.status"        # {cameraId, status, atMs, note?}
ALERT_CREATED = "alert.created"        # objek Alert §4.7
ALERT_RESOLVED = "alert.resolved"      # objek Alert §4.7
ALERT_DELIVERY = "alert.delivery"      # {alertId, equipmentInstanceId, delivery: {channel, target, status, atMs, ...}} — hasil audit kanal push/live per percobaan; hse semua, operator terkait
OPERATOR_PRESENCE = "operator.presence"  # {operatorId, equipmentInstanceId, online} — operator terkait sambung/putus kanal live; hse semua. Sumber realtime HSE "No connection" saat operasi aktif.
CALL_STATE = "call.state"  # panggilan Call Cabin (calls/service.py): {id, equipmentInstanceId, state, endReason, hseId, hseName, operatorId, createdAtMs, answeredAtMs, endedAtMs}; hse semua, operator terkait
ASSIGNMENT_STATUS = "assignment.status"  # assignment + session/shift snapshot
# {equipmentInstanceId, alertIds, response, respondedAtMs, operatorId, operatorName}
# (nama didefinisikan di core.operator_response agar api tidak mengimpor alerts)
from ..core.operator_response import OPERATOR_RESPONSE_EVENT as OPERATOR_RESPONSE  # noqa: E402


class EventBus:
    """Publisher in-process; subscriber = (antrean, role, operator_id)."""

    def __init__(self):
        self.lock = Lock()
        self.subscribers = {}     # id(queue) -> (queue, role, operator_id)
        self.sequence = 0
        self._throttles = {}      # key -> last-sent unix seconds (throttle per key)

    def subscribe(self, listener: Queue, role='hse', operator_id=None) -> None:
        """Daftarkan antrean dengan audiensnya (idempotent per queue)."""
        with self.lock:
            self.subscribers[id(listener)] = (listener, role, operator_id)

    def unsubscribe(self, listener: Queue) -> None:
        """Cabut listener; tanpa error bila tak terdaftar."""
        with self.lock:
            self.subscribers.pop(id(listener), None)

    def online_operators(self):
        """Set operator_id yang punya stream hidup (presence SSE)."""
        with self.lock:
            return {operator_id for _, (_, role, operator_id)
                    in self.subscribers.items()
                    if role == 'operator' and operator_id is not None}

    def publish(self, alert: dict, route_operator_id=None) -> None:
        """Beri nomor urut & dorong ke listener yang berhak menerima.

        route_operator_id: operator dengan binding tervalidasi utk alert ini (None = HSE
        broadcast saja). Listener operator lain tidak menerima apa pun."""
        with self.lock:
            self.sequence += 1
            payload = dict(alert=alert, message=alert["message"], sequence=self.sequence,
                           serverSentAtUnixMs=time.time_ns() // 1_000_000)
            event = ("event: mac-message\ndata: " + json.dumps(payload) + "\n\n").encode()
            for _, (queue, role, operator_id) in list(self.subscribers.items()):
                if role == 'hse' or (role == 'operator'
                                     and operator_id == route_operator_id):
                    queue.put(event)

    def publish_typed(self, event_type: str, payload: dict,
                      route_operator_id=None, throttle_key=None,
                      min_interval_s=None) -> None:
        """Terbitkan event typed (kanal tambahan, dual-emit dengan mac-message).

        Envelope: {"event": event_type, "data": payload, "sequence": int,
        "serverSentAtUnixMs": int}; format SSE `event: <type>` + `data:` JSON.
        Sequence GLOBAL — satu counter yang sama dengan mac-message legacy.
        Audiens identik dengan publish(): hse semua; operator hanya bila
        operator_id == route_operator_id.

        throttle_key + min_interval_s menahan event berulang untuk key yang
        sama (alarm management 16 Nov) — iPhone tidak menerima ratusan event
        alert yang sama. Tanpa key, event tetap real-time.
        """
        if throttle_key is not None and min_interval_s:
            # _throttle_ok sendiri mengunci self.lock — Lock bukan reentrant,
            # jadi dipanggil DI LUAR blok lock publish.
            if not self._throttle_ok(throttle_key, min_interval_s):
                return
        with self.lock:
            self.sequence += 1
            envelope = dict(event=event_type, data=payload, sequence=self.sequence,
                            serverSentAtUnixMs=time.time_ns() // 1_000_000)
            event = ("event: " + event_type
                     + "\ndata: " + json.dumps(envelope) + "\n\n").encode()
            for _, (queue, role, operator_id) in list(self.subscribers.items()):
                if role == 'hse' or (role == 'operator'
                                     and operator_id == route_operator_id):
                    queue.put(event)

    def _throttle_ok(self, key, min_interval_s, now=None):
        """True bila `key` boleh lewat (interval minimum terpenuhi sejak last-sent).

        Menyimpan timestamp last-sent per key; dipakai equipment.status (1/s)
        dan event alert live (lewat publish_typed, alarm management 16 Nov)."""
        with self.lock:
            last = self._throttles.get(key, 0)
            now = now or time.time()
            ok = (now - last) >= min_interval_s
            if ok:
                self._throttles[key] = now
            return ok
