"""Call Cabin (VoIP satu jaringan) — sinyal panggilan + relay suara.

Prototipe: HSE menelepon operator yang sedang terikat (shift_bindings) ke satu
excavator. Status panggilan hidup di memori proses (panggilan tidak perlu
bertahan setelah restart). Suara = frame PCM biner yang direlay server antar
dua kaki (HSE <-> operator) lewat WebSocket `/calls/{id}/audio`; server tidak
menyimpan atau menganalisis audio.

Siklus: ringing -> active -> ended. `endReason`: declined | cancelled |
missed | ended | lost.
"""
import time
import uuid
from queue import Empty, Full, Queue
from threading import Lock

RING_TIMEOUT_S = 30        # tak dijawab -> missed
ORPHAN_TIMEOUT_S = 20      # aktif tapi kedua kaki audio putus -> lost
AUDIO_QUEUE_FRAMES = 50    # ~1 detik @ 20 ms; penuh = buang frame terlama
MAX_AUDIO_FRAME = 4096     # byte per frame audio; lebih besar diabaikan

CALL_STATE_EVENT = 'call.state'


class CallError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Leg:
    """Satu ujung audio: antrean frame keluar menuju socket peserta itu."""

    def __init__(self):
        self.queue = Queue(maxsize=AUDIO_QUEUE_FRAMES)

    def push(self, frame):
        while True:
            try:
                self.queue.put_nowait(frame)
                return
            except Full:
                try:
                    self.queue.get_nowait()   # buang frame terlama, jaga latensi
                except Empty:
                    pass

    def close(self):
        self.push(None)  # sentinel: penulis socket berhenti


class Call:
    def __init__(self, equipment_id, hse, operator_id, now_ms, now_mono):
        self.id = str(uuid.uuid4())
        self.equipment_id = equipment_id
        self.hse_id = hse['id']
        self.hse_name = hse.get('display_name') or hse.get('username') or ''
        self.operator_id = operator_id
        self.state = 'ringing'
        self.end_reason = None
        self.created_ms = now_ms
        self.answered_ms = None
        self.ended_ms = None
        self.created_mono = now_mono
        self.orphan_since = None      # saat aktif tanpa kaki audio terhubung
        self.legs = {}                # 'hse' | 'operator' -> Leg

    def role_of(self, user):
        if user['id'] == self.hse_id and user['role'] == 'hse':
            return 'hse'
        if user['id'] == self.operator_id and user['role'] == 'operator':
            return 'operator'
        return None

    def public(self):
        return {
            'id': self.id,
            'equipmentInstanceId': self.equipment_id,
            'state': self.state,
            'endReason': self.end_reason,
            'hseId': self.hse_id,
            'hseName': self.hse_name,
            'operatorId': self.operator_id,
            'createdAtMs': self.created_ms,
            'answeredAtMs': self.answered_ms,
            'endedAtMs': self.ended_ms,
        }


class CallService:
    """`resolve_operator(equipment_id)` -> operator_id | None (binding shift)."""

    def __init__(self, bus, resolve_operator, clock=time.monotonic,
                 wall_ms=lambda: time.time_ns() // 1_000_000,
                 ring_timeout_s=RING_TIMEOUT_S, orphan_timeout_s=ORPHAN_TIMEOUT_S):
        self._bus = bus
        self._resolve_operator = resolve_operator
        self._clock = clock
        self._wall_ms = wall_ms
        self._ring_timeout_s = ring_timeout_s
        self._orphan_timeout_s = orphan_timeout_s
        self._lock = Lock()
        self._calls = {}

    # --- sinyal -----------------------------------------------------------

    def start(self, hse, equipment_id):
        operator_id = self._resolve_operator(equipment_id)
        with self._lock:
            self._expire_locked()
            if any(call.equipment_id == equipment_id and call.state != 'ended'
                   for call in self._calls.values()):
                raise CallError('call_in_progress')
            if operator_id is None:
                raise CallError('no_operator')
            if operator_id not in self._bus.online_operators():
                raise CallError('operator_offline')
            call = Call(equipment_id, hse, operator_id, self._wall_ms(), self._clock())
            self._calls[call.id] = call
            payload = call.public()
        self._publish(call, payload)
        return payload

    def get(self, call_id, user):
        with self._lock:
            self._expire_locked()
            call = self._participant_call(call_id, user)
            return call.public()

    def answer(self, call_id, user):
        with self._lock:
            self._expire_locked()
            call = self._participant_call(call_id, user)
            if call.role_of(user) != 'operator':
                raise CallError('forbidden')
            if call.state != 'ringing':
                raise CallError('invalid_state')
            call.state = 'active'
            call.answered_ms = self._wall_ms()
            call.orphan_since = self._clock()
            payload = call.public()
        self._publish(call, payload)
        return payload

    def decline(self, call_id, user):
        with self._lock:
            self._expire_locked()
            call = self._participant_call(call_id, user)
            if call.role_of(user) != 'operator':
                raise CallError('forbidden')
            if call.state != 'ringing':
                raise CallError('invalid_state')
            payload = self._finish_locked(call, 'declined')
        self._publish(call, payload)
        return payload

    def end(self, call_id, user):
        """Akhiri panggilan; idempoten bila sudah selesai."""
        with self._lock:
            self._expire_locked()
            call = self._participant_call(call_id, user)
            if call.state == 'ended':
                return call.public()
            role = call.role_of(user)
            if call.state == 'ringing':
                reason = 'cancelled' if role == 'hse' else 'declined'
            else:
                reason = 'ended'
            payload = self._finish_locked(call, reason)
        self._publish(call, payload)
        return payload

    # --- audio ------------------------------------------------------------

    def open_audio(self, call_id, user):
        """Buka kaki audio peserta. Mengembalikan (call, role, leg)."""
        with self._lock:
            self._expire_locked()
            call = self._participant_call(call_id, user)
            if call.state != 'active':
                raise CallError('invalid_state')
            role = call.role_of(user)
            old = call.legs.get(role)
            leg = Leg()
            call.legs[role] = leg
            call.orphan_since = None
        if old is not None:
            old.close()   # sambungan baru menggantikan yang lama
        return call, role, leg

    def relay(self, call, role, frame):
        """Teruskan satu frame ke peserta lawan (dibuang bila lawan belum tersambung)."""
        if not frame or len(frame) > MAX_AUDIO_FRAME:
            return False
        peer = call.legs.get('operator' if role == 'hse' else 'hse')
        if peer is None or call.state != 'active':
            return False
        peer.push(frame)
        return True

    def close_audio(self, call, role, leg):
        with self._lock:
            if call.legs.get(role) is leg:
                del call.legs[role]
                if call.state == 'active' and not call.legs:
                    call.orphan_since = self._clock()

    def is_active(self, call):
        with self._lock:
            self._expire_locked()
            return call.state == 'active'

    # --- internal ---------------------------------------------------------

    def _participant_call(self, call_id, user):
        call = self._calls.get(call_id)
        if call is None:
            raise CallError('not_found')
        if call.role_of(user) is None:
            raise CallError('forbidden')
        return call

    def _finish_locked(self, call, reason):
        call.state = 'ended'
        call.end_reason = reason
        call.ended_ms = self._wall_ms()
        for leg in list(call.legs.values()):
            leg.close()
        call.legs.clear()
        return call.public()

    def _expire_locked(self):
        now = self._clock()
        expired = []
        for call in self._calls.values():
            if call.state == 'ringing' and now - call.created_mono > self._ring_timeout_s:
                expired.append((call, self._finish_locked(call, 'missed')))
            elif (call.state == 'active' and call.orphan_since is not None
                  and now - call.orphan_since > self._orphan_timeout_s):
                expired.append((call, self._finish_locked(call, 'lost')))
        # Panggilan selesai >5 menit tidak perlu disimpan di memori.
        wall = self._wall_ms()
        for call_id in [i for i, c in self._calls.items()
                        if c.state == 'ended' and wall - c.ended_ms > 300_000]:
            del self._calls[call_id]
        for call, payload in expired:
            self._publish(call, payload)

    def _publish(self, call, payload):
        publish = getattr(self._bus, 'publish_typed', None)
        if publish is not None:
            publish(CALL_STATE_EVENT, payload, route_operator_id=call.operator_id)


def operator_for_equipment(connection_factory, equipment_id):
    """Operator yang sedang terikat shift ke unit ini (sesi masih berlaku)."""
    with connection_factory() as connection:
        row = connection.execute(
            'SELECT b.operator_id FROM shift_bindings b '
            'JOIN sessions s ON s.user_id=b.operator_id AND s.expires_at>now() '
            'WHERE b.equipment_instance_id=%s LIMIT 1',
            (equipment_id,)).fetchone()
    return str(row[0]) if row else None
