"""Domain murni — evaluator transisi zona bahaya per-unit + histeresis ganda.

Mode zona (dipilih otomatis dari ketersediaan kalibrasi):
- METER  — bila RiskEvaluator dikonstruksi dengan `calibration=`: person
  masuk zona bila titik tanahnya <= radius dari titik tanah excavator mana
  pun di unit itu (lihat core/zones.py). Histeresis JARAK: zona confirm
  memakai zone_radius_m (default 4 m, keputusan owner 8 Nov 2026;
  --zone-radius/ZONE_RADIUS_M), zona aktif dipertahankan sampai keluar
  clear_radius_m (default 6 m) — person di band 4–6 m tidak mematikan
  zona aktif.
- PIKSEL — fallback tanpa kalibrasi: bounding box excavator di-inflate
  PADDING_RATIO tiap sisi (histeresis jarak tidak tersedia di mode ini).

Unit / identitas (M4): excavator dapat membawa `trackId` (kolom opsional
Detection) — jadi kunci `equipmentInstanceId`; state hazard/count/jeda
TERPISAH per unit (jeda alert per unit = keputusan produk: alert unit A
tidak menahan unit B). Excavator tanpa trackId dikelompokkan jadi SATU
zona anonim per sumber (placeholder sampai modul tracking nyata; campuran
tracked+anonim berjalan sebagai unit terpisah). Identitas anonim dapat
di-override composition root via `identity_for` (seam 14 Nov 2026): unit
ter-DAFTAR registry menang atas fallback prefix kamera — kamera yang
di-relink tetap ber-pasangan dengan assignment/radius/fleet di app.
Person yang berada di
radius >1 unit terhitung di tiap unitnya (alert terpisah — rute operator
berbeda). Unit yang deteksinya absen saat beroperasi menjadi unknown, bukan
clear. Transisi simultan
lintas unit masuk antrean FIFO — satu alert per frame, tidak ada event
hilang (sisa antrean di akhir video belum diflush — menyusul bersama
riwayat alert M6).

Transisi STATUS dikontrol histeresis WAKTU (core/hazard.py, port workspace
fix 7 Nov): CONFIRMED setelah dalam zona >= confirm_time_s (default 0.25 s),
CLEARED setelah luar zona >= clear_time_s (default 0.5 s), voting window 1 s
— satu frame tunggal tidak pernah mengubah status. Alert hanya saat
transisi. Kebijakan 12 Nov (fast alert): person_entered_zone /
person_high_risk / zone_cleared TERBIT LANGSUNG pada transisi tanpa jeda
dan tanpa pending — konfirmasi tidak pernah ditelan sehingga tidak ada
cycle senyap (swallow = false negative). Jeda min_alert_gap_ms kini hanya
berlaku untuk person_count_changed, plus syarat stabil count_stable_s.

High-risk (keputusan owner 8-9 Nov 2026): zona nested di dalam zona regulasi
dengan radius = ½ regulasi (high_risk_ratio default 0.5; regulasi 4 m ->
high-risk 2 m). HANYA mode meter — tanpa kalibrasi tidak ada kind high-risk.
HazardState kedua (high_hazard) men-drive transisi masuk high-risk ->
`person_high_risk` (personsInZone = jumlah person dalam radius high-risk);
keluar high-risk (tapi masih dalam zona) kembali ke kinds normal, dan
`zone_cleared` menutup semua level — tanpa kind `high_risk_cleared` terpisah.

Radius per unit (keputusan owner 11 Nov 2026, slider "Safety Zone" app HSE):
`radius_for(equipmentInstanceId) -> meter | None` disuntik composition root.
None = pakai zone_radius_m global. Mode meter: radius confirm = nilai unit,
radius clear & high-risk diskalakan dengan rasio global yang sama (6/4,
½). Mode piksel: padding bbox diskalakan sebanding (radius_unit /
zone_radius_m) — APROKSIMASI tanpa kalibrasi, bukan jarak meter sungguhan.

Batasan yang disengaja: kalibrasi via injeksi duck-typed (`to_meter`) —
evaluator tetap numpy-free; modul ini stdlib saja, tidak mengimpor modul
Sources lain di luar core (aturan dependency clean-lite, CONTEXT.md).
"""
import time

from .detection import CLASS_EXCAVATOR, CLASS_PERSON
from .hazard import CLEARED, CONFIRMED, CLEAR_TIME_S, CONFIRM_TIME_S, WINDOW_S, HazardState
from .zones import (DEFAULT_CLEAR_RADIUS_M, DEFAULT_HIGH_RISK_RATIO,
                    DEFAULT_ZONE_RADIUS_M, high_risk_radius,
                    persons_within_high_risk, persons_within_radius)

PADDING_RATIO = 0.25
MIN_ALERT_GAP_MS = 2000


def _inflated(box, ratio):
    x1, y1, x2, y2 = box
    dx = (x2 - x1) * ratio
    dy = (y2 - y1) * ratio
    return (x1 - dx, y1 - dy, x2 + dx, y2 + dy)


def _center_inside(point, box):
    x1, y1, x2, y2 = box
    return x1 <= point[0] <= x2 and y1 <= point[1] <= y2


def _persons_in_zone_pixels(excavators, persons, padding_ratio):
    if not excavators or not persons:
        return 0
    zones = [_inflated(d['box'], padding_ratio) for d in excavators]
    count = 0
    for person in persons:
        x1, y1, x2, y2 = person['box']
        center = ((x1 + x2) / 2, (y1 + y2) / 2)
        count += 1 if any(_center_inside(center, zone) for zone in zones) else 0
    return count


class RiskEvaluator:
    """Evaluator transisi zona dengan state per sumber dan per unit.

    Instance dirakit oleh composition root (AlertServer/main.py) atau
    pengujian; dua instance tidak berbagi state. Parameter (mode, radius
    confirm/clear, waktu histeresis, jeda alert) bisa disuntik.
    """

    def __init__(self, padding_ratio=PADDING_RATIO, min_alert_gap_ms=MIN_ALERT_GAP_MS,
                 zone_radius_m=DEFAULT_ZONE_RADIUS_M, calibration=None,
                 clear_radius_m=DEFAULT_CLEAR_RADIUS_M,
                 high_risk_ratio=DEFAULT_HIGH_RISK_RATIO,
                 confirm_time_s=CONFIRM_TIME_S, clear_time_s=CLEAR_TIME_S, window_s=WINDOW_S,
                 radius_for=None, operating_for=None, calibration_for=None,
                 identity_for=None, count_stable_s=1.0, clear_hold_s=0.0,
                 active_hazard_for=None, operation_started_for=None):
        if not zone_radius_m > 0:
            raise ValueError('zone_radius_m harus angka positif (meter)')
        if clear_radius_m < zone_radius_m:
            raise ValueError('clear_radius_m (histeresis) harus >= zone_radius_m')
        if not 0 < high_risk_ratio < 1:
            raise ValueError('high_risk_ratio harus di antara 0 dan 1 (setengah radius)')
        if clear_hold_s < 0:
            raise ValueError('clear_hold_s harus angka >= 0 (detik)')
        self.padding_ratio = padding_ratio
        # Semantik 12 Nov: min_alert_gap_ms HANYA jeda `person_count_changed`
        # (bukan event bahaya). Entry/high-risk/cleared terbit pada transisi
        # HazardState tanpa ditunda — fast alert + zero swallow.
        self.min_alert_gap_ms = min_alert_gap_ms
        # Perubahan jumlah person baru terbit bila jumlah BARU stabil selama
        # count_stable_s (mencegah flicker hitungan nie-cancel).
        self.count_stable_s = count_stable_s
        self.zone_radius_m = zone_radius_m
        self.clear_radius_m = clear_radius_m
        self.high_risk_ratio = high_risk_ratio
        self.calibration = calibration  # duck-typed to_meter; None = zona piksel
        # callable(source_id) -> calibration | None — kalibrasi PER KAMERA
        # (Server/calibrations/{Nama Kamera}.json). Prioritas di atas
        # `calibration` global; kamera tanpa calib -> zona piksel fallback.
        self.calibration_for = calibration_for
        self.confirm_time_s = confirm_time_s
        self.clear_time_s = clear_time_s
        self.window_s = window_s
        # Zona harus kosong selama clear_hold_s detik BERTURUT-TURUT sebelum
        # insiden ditutup (anti kedip deteksi). 0 = perilaku lama; server
        # meneruskan --clear-hold (default CLEAR_HOLD_S=3 s) lewat composition root.
        self.clear_hold_s = clear_hold_s
        self.radius_for = radius_for  # callable(equipmentInstanceId) -> meter | None
        self.operating_for = operating_for  # callable(equipmentInstanceId) -> bool
        self.active_hazard_for = active_hazard_for
        self.operation_started_for = operation_started_for
        # Sem identitas (keputusan owner 14 Nov 2026): callable(source_id) ->
        # equipmentInstanceId ter-DAFTAR bila kameranya ter-link registry
        # (relink RTSP/pendaftaran ulang); None/unknown -> fallback prefix.
        # trackId nyata tetap menang — resolver hanya untuk zona anonim.
        self.identity_for = identity_for
        self._state = {}

    def _calibration(self, source_id):
        """Kalibrasi untuk SATU sumber: per-kamera bila ada, else global."""
        if self.calibration_for is not None:
            return self.calibration_for(source_id)
        return self.calibration

    def _hazard_state(self):
        """State machine bahaya satu unit (zona regulasi atau high-risk)."""
        return HazardState(self.confirm_time_s, self.clear_time_s, self.window_s,
                           self.clear_hold_s)

    def reset(self):
        """Kosongkan state transisi (dipakai pengujian)."""
        self._state.clear()

    def _identity(self, key, source_id):
        """equipmentInstanceId unit: trackId; else resolver registry
        (identitas kamera→unit, seam 14 Nov); else fallback anonim per sumber."""
        if key is not None:
            return key
        if self.identity_for is not None:
            resolved = self.identity_for(source_id)
            if resolved:
                return resolved
        return f'{str(source_id or "camera")[:8]}-eq-1'

    def unit_radius_m(self, identity):
        """Radius confirm unit (meter): nilai per unit bila ada, else global."""
        if self.radius_for is not None:
            value = self.radius_for(identity)
            if value is not None and value > 0:
                return float(value)
        return self.zone_radius_m

    def _in_zone_count(self, excavators, persons, active, radius_m=None, source_id=None):
        # Operator (person di dalam bbox excavator) tidak pernah dihitung —
        # filter di sini juga supaya panggilan metode langsung aman.
        persons = [p for p in persons if not p.get('operator')]
        if not excavators or not persons:
            return 0
        radius_m = radius_m or self.zone_radius_m
        scale = radius_m / self.zone_radius_m
        calibration = self._calibration(source_id)
        if calibration is None:
            return _persons_in_zone_pixels(excavators, persons, self.padding_ratio * scale)
        # Histeresis jarak: zona AKTIF dipertahankan sampai person keluar
        # radius clear (6 m); zona BARU dikonfirmasi mulai radius confirm (4 m).
        radius = self.clear_radius_m * scale if active else radius_m
        return persons_within_radius(persons, excavators, calibration, radius)

    def _high_risk_radius_m(self, radius_m=None):
        return high_risk_radius(radius_m or self.zone_radius_m, self.high_risk_ratio)

    def _in_high_risk_count(self, excavators, persons, radius_m=None, source_id=None):
        """Jumlah person dalam zona high-risk (½ regulasi). Hanya mode meter."""
        persons = [p for p in persons if not p.get('operator')]
        calibration = self._calibration(source_id)
        if calibration is None or not excavators or not persons:
            return 0
        return persons_within_high_risk(persons, excavators, calibration,
                                        self._high_risk_radius_m(radius_m))

    def _advance_unit(self, unit, key, in_zone, in_high, now, timestamp_ms, source_id=None,
                     calibration=None):
        # Fallback identitas per sumber (P3) via seam resolver: unit ter-DAFTAR
        # (relink kamera) dipetakan composition root; tanpa resolver tetap
        # "{prefix-8}-eq-1" supaya alert/types bisa dirute; adapter tracking
        # nyata (trackId) kelak menggantikan fallback ini.
        alert_key = self._identity(key, source_id)
        """Satu langkah state-machine satu unit; return dict alert atau None.

        Dua hazard bersarang (keputusan 13): `hazard` = zona regulasi,
        `high_hazard` = high-risk (hanya mode meter). Entry ke high-risk
        terbit `person_high_risk` (menekan entry normal bila keduanya konfirmasi
        pada frame yang sama); exit high-risk kembali ke kinds normal; tidak ada
        kind `high_risk_cleared` — `zone_cleared` menutup semua level.
        """
        event = unit['hazard'].update(in_zone > 0, now / 1000.0)
        high_event = None
        if calibration is not None:
            high_event = unit['high_hazard'].update(in_high > 0, now / 1000.0)

        if event == CLEARED:
            # Zona benar-benar kosong (keluar clear radius) — menutup SEMUA
            # level termasuk high-risk. Kebijakan 12 Nov (fast alert): cleared
            # SELALU terbit — konfirmasi tidak pernah ditahan, jadi tidak ada
            # lagi cycle senyap yang tertelan (swallow = false negative).
            unit['active'] = False
            unit['active_since_ms'] = None
            unit['hazard_active_10s_sent'] = False
            unit['high_count'] = 0
            return self._alert(unit, alert_key, 'zone_cleared',
                               'Zona excavator kembali kosong', 0,
                               now, timestamp_ms, source_id)

        if high_event == CONFIRMED:
            # Masuk high-risk — `person_high_risk`, terbit LANGSUNG pada
            # transisi (kebijakan 12 Nov: event bahaya tak pernah ditunda).
            unit['active'] = True
            if unit.get('active_since_ms') is None:
                unit['active_since_ms'] = now
            unit['high_count'] = in_high
            unit['count'] = in_zone
            return self._high_risk_alert(unit, alert_key, in_high, now, timestamp_ms, source_id)

        if event == CONFIRMED:
            # `HazardState` hanya memancarkan CONFIRMED sekali per episode
            # masuk, jadi event bahaya sudah alami transisi-driven — tak ada
            # jeda, tak ada pending (kebijakan 12 Nov: fast alert).
            unit['active'] = True
            if unit.get('active_since_ms') is None:
                unit['active_since_ms'] = now
            return self._alert(unit, alert_key, 'person_entered_zone',
                               'Person terdeteksi di zona excavator', in_zone,
                               now, timestamp_ms, source_id)

        if high_event == CLEARED:
            # Keluar high-risk tapi masih dalam zona regulasi — kembali ke kinds
            # normal (tanpa alert khusus; zona tetap aktif).
            unit['high_count'] = 0
            unit['count_since_ms'] = None

        # Perubahan jumlah person bukan event bahaya (kebijakan 12 Nov):
        # baru terbit setelah count STABIL (count_stable_s) DAN jeda
        # min_alert_gap_ms sejak alert terakhir unit — redaman banjir
        # person_count_changed yang dulu sampai 52% riwayat. Count di-update
        # saat terbit.
        if unit['active'] and in_zone > 0 and in_zone != unit['count']:
            if unit.get('count_since') != in_zone:
                unit['count_since'] = in_zone
                unit['count_since_ms'] = now
                return None
            if (now - (unit.get('count_since_ms') or 0)) >= self.count_stable_s * 1000 \
                    and now - unit['last'] >= self.min_alert_gap_ms:
                unit['count'] = in_zone
                return self._alert(unit, alert_key, 'person_count_changed',
                                   f'Person di zona excavator berubah menjadi {in_zone}',
                                   in_zone, now, timestamp_ms, source_id)
        active_since = unit.get('active_since_ms')
        if (unit['active'] and unit.get('operation_active', True) and in_zone > 0
                and active_since is not None and not unit.get('hazard_active_10s_sent')
                and now - active_since >= 10_000):
            unit['hazard_active_10s_sent'] = True
            return self._alert(unit, alert_key, 'hazard_active_10s',
                               'Hazard still active for 10 s', in_zone,
                               now, timestamp_ms, source_id)
        return None

    def evaluate(self, detections, timestamp_ms=None, source_id=None, now_ms=None):
        """Return SATU dict alert per panggilan (antrean FIFO), atau None."""
        now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        source_state = self._state.setdefault(source_id or 'recording',
                                               {'units': {}, 'queued': []})
        excavators = [d for d in detections if d.get('classId') == CLASS_EXCAVATOR]
        # Person bertanda operator (di dalam bbox excavator, keputusan 11 Nov)
        # bukan bahaya — keluar dari evaluasi zona/high-risk.
        persons = [d for d in detections
                   if d.get('classId') == CLASS_PERSON and not d.get('operator')]

        # Kelompokkan excavator per identitas track; tanpa trackId = satu
        # zona anonim per sumber (placeholder — CONTEXT P3: koordinat bukan
        # identitas, jadi TIDAK ada key posisi).
        groups = {}
        for excavator in excavators:
            groups.setdefault(excavator.get('trackId'), []).append(excavator)

        alerts = []
        # Union: unit yang absen tetap disimpan agar alert aktif tidak hilang.
        keys = set(groups) | set(source_state['units'])
        if source_id is not None:
            keys.add(None)
        for key in sorted(keys, key=lambda k: (k is None, k or '')):
            unit = source_state['units'].get(key)
            if unit is None:
                unit = {'hazard': self._hazard_state(),
                        'high_hazard': self._hazard_state(),
                        'active': False, 'count': 0, 'high_count': 0, 'last': 0,
                        'count_since': None, 'count_since_ms': None,
                        'active_since_ms': None, 'hazard_active_10s_sent': False}
                if self.active_hazard_for is not None:
                    snapshot = self.active_hazard_for(self._identity(key, source_id))
                    if snapshot:
                        unit['active'] = True
                        unit['active_since_ms'] = int(snapshot[0])
                        started = (self.operation_started_for(self._identity(key, source_id))
                                   if self.operation_started_for is not None else None)
                        if started is not None:
                            unit['operation_started_at_ms'] = started
                            unit['active_since_ms'] = max(unit['active_since_ms'], started)
                        unit['hazard_active_10s_sent'] = (snapshot[1] is not None
                            and (started is None or snapshot[1] >= started))
                        unit['hazard'].status = CONFIRMED
                        unit['high_hazard'].status = CONFIRMED
                        unit['hazard'].rebase(now / 1000.0, True)
                        unit['high_hazard'].rebase(now / 1000.0, True)
                source_state['units'][key] = unit
            identity = self._identity(key, source_id)
            operating = (self.operating_for(identity)
                         if self.operating_for is not None else True)
            unit['operation_active'] = operating
            started = (self.operation_started_for(identity)
                       if operating and self.operation_started_for is not None else None)
            if started is not None and unit.get('operation_started_at_ms') != started:
                unit['operation_started_at_ms'] = started
                if unit.get('active'):
                    unit['active_since_ms'] = max(started,
                                                  unit.get('active_since_ms') or started)
                    unit['hazard_active_10s_sent'] = False
            # Setelah operasi selesai, hazard yang sudah aktif tetap diamati
            # sampai kamera benar-benar menerbitkan zone_cleared. Hazard baru
            # tidak boleh dimulai saat unit tidak beroperasi.
            if not operating and not unit.get('active'):
                unit['hazard'] = self._hazard_state()
                unit['high_hazard'] = self._hazard_state()
                unit.update(active=False, count=0, high_count=0, count_since=None,
                            count_since_ms=None, last_in_zone=0, last_in_high=0,
                            active_since_ms=None, hazard_active_10s_sent=False,
                            monitoring_status='idle', last_seen_ms=now)
                continue

            unit['monitoring_status'] = 'online' if groups.get(key) else 'unknown'
            if not groups.get(key):
                # Absence of the excavator is an observation failure, not proof
                # that its swing zone is clear.
                unit['last_in_zone'] = None
                unit['last_in_high'] = None
                unit['observation_interrupted'] = True
                continue

            radius_m = self.unit_radius_m(identity)
            calibration = self._calibration(source_id)
            in_zone = self._in_zone_count(groups.get(key, []), persons, unit['active'],
                                           radius_m, source_id)
            in_high = self._in_high_risk_count(groups.get(key, []), persons, radius_m,
                                               source_id)
            if unit.pop('observation_interrupted', False):
                unit['hazard'].rebase(now / 1000.0, in_zone > 0)
                unit['high_hazard'].rebase(now / 1000.0, in_high > 0)
            # Simpan in_zone/in_high untuk unit_statuses() (integration typed event).
            unit['last_in_zone'] = in_zone
            unit['last_in_high'] = in_high
            alert = self._advance_unit(unit, key, in_zone, in_high, now, timestamp_ms,
                                       source_id, calibration)
            if alert is not None:
                alerts.append(alert)

        source_state['queued'].extend(alerts)
        result = source_state['queued'].pop(0) if source_state['queued'] else None

        # Update last seen per unit untuk unit_statuses().
        for key, unit in source_state['units'].items():
            if unit.get('monitoring_status') == 'online':
                unit['last_seen_ms'] = now

        return result

    def unit_statuses(self, source_id=None):
        """ZoneStatus per unit (api-draft 4.4) untuk typed event equipment.status.

        Return list {equipmentInstanceId, zoneStatus, active, lastInZone,
        lastInHigh, lastSeenMs} — zoneStatus tri-level dari hazard state.
        Identitas via seam yang sama dengan payload alert (trackId → resolver
        registry → fallback prefix per sumber)."""
        state = self._state.get(source_id or 'recording', {})
        result = []
        for key, unit in state.get('units', {}).items():
            identity = self._identity(key, source_id)
            result.append({
                'equipmentInstanceId': identity,
                'zoneStatus': self._derive_zone_status(unit),
                'monitoringStatus': unit.get('monitoring_status', 'unknown'),
                'operationActive': unit.get('operation_active', False),
                'active': unit.get('active', False),
                'lastInZone': unit.get('last_in_zone', 0),
                'lastInHigh': unit.get('last_in_high', 0),
                'lastSeenMs': unit.get('last_seen_ms'),
            })
        return result

    @staticmethod
    def _derive_zone_status(unit):
        """ZoneStatus tri-level dari state hazard: danger > warning > clear."""
        if unit.get('monitoring_status') == 'unknown':
            return 'unknown'
        if unit.get('monitoring_status') == 'idle':
            return 'clear'
        high_hazard = unit.get('high_hazard')
        if high_hazard is not None and high_hazard.status == 'confirmed':
            return 'danger'
        if unit.get('active'):
            return 'warning'
        return 'clear'

    def mark_source_unobserved(self, source_id, now_ms=None):
        """Mark a source unavailable while preserving active hazard state."""
        now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        state = self._state.setdefault(source_id or 'recording', {'units': {}, 'queued': []})
        key = None
        unit = state['units'].get(key)
        if unit is None:
            unit = {'hazard': self._hazard_state(),
                    'high_hazard': self._hazard_state(),
                    'active': False, 'count': 0, 'high_count': 0, 'last': 0,
                    'count_since': None, 'count_since_ms': None}
            state['units'][key] = unit
        identity = self._identity(key, source_id)
        operating = (self.operating_for(identity)
                     if self.operating_for is not None else True)
        unit['operation_active'] = operating
        unit['monitoring_status'] = 'unknown' if operating else 'idle'
        unit['last_in_zone'] = None if operating else 0
        unit['last_in_high'] = None if operating else 0
        if operating:
            unit['observation_interrupted'] = True
        else:
            unit['active'] = False
            unit['hazard'] = self._hazard_state()
            unit['high_hazard'] = self._hazard_state()
        return self.unit_statuses(source_id)

    @staticmethod
    def _alert(unit, key, kind, message, persons, now, timestamp_ms, source_id):
        unit['active'] = kind != 'zone_cleared'
        unit['count'] = persons
        unit['last'] = now
        payload = {
            'message': message,
            'kind': kind,
            'simulation': True,
            'sourceId': source_id,
            'personsInZone': persons,
            'frameTimestampMs': timestamp_ms,
        }
        if key is not None:
            # Kontrak additive (draft Contracts Alert): identitas unit stabil —
            # hanya ada bila tracking menyediakannya; zona anonim tanpa field.
            payload['equipmentInstanceId'] = key
        return payload

    @staticmethod
    def _high_risk_alert(unit, key, high_persons, now, timestamp_ms, source_id):
        """Alert person_high_risk — payload standar 6-field; personsInZone =
        jumlah person dalam radius HIGH-RISK (bukan zona regulasi)."""
        unit['last'] = now
        payload = {
            'message': 'Person terlalu dekat excavator (high risk)',
            'kind': 'person_high_risk',
            'simulation': True,
            'sourceId': source_id,
            'personsInZone': high_persons,
            'frameTimestampMs': timestamp_ms,
        }
        if key is not None:
            payload['equipmentInstanceId'] = key
        return payload
