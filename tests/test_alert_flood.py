"""Test anti-flood alert (alarm management 16 Nov 2026).

Jaminan yang dikunci di sini:
(1) clear-hold 3 s — kedip deteksi 1-2 detik bukan insiden baru;
(2) composition root meneruskan --clear-hold ke evaluator;
(3) gerbang push — satu push sejenis per unit per jeda, eskalasi selalu lolos;
(4) pengingat 5 menit berhenti setelah operator merespons;
(5) event live ke iOS di-throttle per (unit + kind).
"""
import sys
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.alerts.bus import EventBus
from Sources.alerts.server import AlertServer
from Sources.core.evaluator import RiskEvaluator
from Sources.core.hazard import CLEARED, CONFIRMED, HazardState


def _in_zone_samples(spans, step=0.125, until=12.0):
    """Sampel 8 fps: orang di zona pada rentang waktu `spans` (detik)."""
    def inside(t):
        return any(start <= t < end for start, end in spans)
    t = 0.0
    while t <= until:
        yield t, inside(t)
        t += step


class ClearHoldTests(unittest.TestCase):
    def test_flicker_short_absence_is_one_incident(self):
        """Orang bolak-balik di tepi zona (tiap 0,8 s keluar) = SATU insiden."""
        state = HazardState(0.25, 0.75, 1.0, 3.0)
        spans = [(0.0, 0.4), (1.2, 1.6), (2.4, 2.8), (3.6, 4.0)]
        events = [(t, event) for t, inside in _in_zone_samples(spans)
                  for event in [state.update(inside, t)] if event]

        self.assertEqual([event for _, event in events].count(CONFIRMED), 1,
                         'kedip deteksi tidak boleh jadi insiden baru')
        self.assertEqual([event for _, event in events].count(CLEARED), 1)
        # Insiden baru hanya setelah zona kosong 3 s berturut-turut (keluar 4,0 s
        # -> CLEARED sekitar 6,9 s).
        cleared_at = next(t for t, event in events if event == CLEARED)
        self.assertGreaterEqual(cleared_at, 6.5)

    def test_without_clear_hold_the_same_flicker_produces_two_incidents(self):
        """Regresi guard: tanpa clear-hold, pola sama jadi 2 insiden."""
        state = HazardState(0.25, 0.75, 1.0, 0.0)
        spans = [(0.0, 0.4), (1.2, 1.6), (2.4, 2.8), (3.6, 4.0)]
        events = [state.update(inside, t) for t, inside in _in_zone_samples(spans)]
        confirmed = [event for event in events if event == CONFIRMED]
        self.assertEqual(len(confirmed), 2)

    def test_evaluator_default_stays_backward_compatible(self):
        """Default evaluator 0 (perilaku lama); server yang mengaktifkan 3 s."""
        self.assertEqual(RiskEvaluator().clear_hold_s, 0.0)
        self.assertEqual(RiskEvaluator(clear_hold_s=3.0)._hazard_state().clear_hold_s, 3.0)

    def test_composition_root_passes_clear_hold(self):
        """--clear-hold (default CLEAR_HOLD_S=3 s) diteruskan ke evaluator."""
        import io
        from contextlib import redirect_stdout
        from Sources.config import parse_args
        from Sources.main import build_risk
        argv = sys.argv
        try:
            sys.argv = ['server']
            args = parse_args()
        finally:
            sys.argv = argv
        self.assertEqual(args.clear_hold, 3.0)
        with redirect_stdout(io.StringIO()):
            evaluator = build_risk(args)
        self.assertEqual(evaluator.clear_hold_s, 3.0)


class _GateHolder:
    """Pemakai method gerbang push tanpa socket/DB (unit test murni)."""

    _push_allowed = AlertServer._push_allowed
    _note_alert_volume = AlertServer._note_alert_volume

    def __init__(self):
        self._push_gate = {}
        self._alert_volume = {}


class PushGateTests(unittest.TestCase):
    def test_same_kind_pushes_once_per_cooldown(self):
        gate = _GateHolder()
        allowed = [gate._push_allowed('person_entered_zone', 'u1', i * 6_000)
                   for i in range(10)]
        self.assertEqual(allowed.count(True), 1)
        self.assertEqual(allowed.count(False), 9)

    def test_escalation_high_risk_always_passes(self):
        gate = _GateHolder()
        allowed = [gate._push_allowed('person_high_risk', 'u1', i * 6_000)
                   for i in range(5)]
        self.assertEqual(allowed.count(True), 5)

    def test_cooldown_is_per_unit(self):
        gate = _GateHolder()
        self.assertTrue(gate._push_allowed('person_entered_zone', 'u1', 0))
        self.assertTrue(gate._push_allowed('person_entered_zone', 'u2', 1_000))

    def test_flood_extends_cooldown(self):
        """Saat flooding, jeda push memanjang 5 menit — push PERTAMA tetap lolos.

        Push pertama wajib selalu lolos (operator harus diberi tahu); yang
        tertunda adalah bunyi berikutnya untuk unit yang sama."""
        gate = _GateHolder()
        now = 0
        for _ in range(12):
            now += 30_000
            flood = gate._note_alert_volume('u1', now)
        self.assertTrue(flood, 'volume >10 alert/10 menit = flooding')
        self.assertTrue(gate._push_allowed('person_entered_zone', 'u1', now, flood),
                        'push pertama tetap lolos walau flooding')
        # 2,5 menit kemudian: tanpa flood jeda 120 s sudah cukup, dengan flood
        # jeda 300 s belum -> bunyi kedua ditahan.
        self.assertFalse(gate._push_allowed('person_entered_zone', 'u1',
                                            now + 150_000, flood))
        # Tanpa flag flood, jarak yang sama lolos.
        self.assertTrue(gate._push_allowed('person_entered_zone', 'u2',
                                           now + 150_000, False))


class _ReminderHolder:
    """Pemakai logika pengingat dengan store tiruan."""

    _send_due_reminders = AlertServer._send_due_reminders

    def __init__(self, rows):
        self._push_gate = {}
        self._reminders = {}
        self._push_sent = []
        self.context = type('Ctx', (), {})()
        self.context.store = type('Store', (), {'active_danger_units': staticmethod(
            lambda: list(rows))})()

    def _route_push_operator(self, stored):
        return 'operator-1'

    def _push(self, stored, operator_id, now_ms):
        self._push_sent.append(now_ms)


class ReminderTests(unittest.TestCase):
    def _holder(self, operator_response=None):
        rows = [{'id': 'a1', 'equipmentInstanceId': 'u1', 'personsInZone': 1,
                 'operatorResponse': operator_response}]
        holder = _ReminderHolder(rows)
        holder._push_gate[('u1', 'person_entered_zone')] = 1_000_000_000
        return holder

    def _run(self, holder, minute):
        with patch('Sources.alerts.server.time.time_ns',
                   return_value=(1_000_000_000 + minute * 60_000) * 1_000_000):
            holder._send_due_reminders()

    def test_reminder_every_five_minutes_max_three(self):
        holder = self._holder()
        for minute in (1, 3, 6, 11, 16, 21):
            self._run(holder, minute)
        self.assertEqual(len(holder._push_sent), 3,
                         'pengingat tiap 5 menit, maksimal 3 per insiden')

    def test_reminder_stops_after_operator_response(self):
        holder = self._holder(operator_response='stopped')
        for minute in (6, 11, 16):
            self._run(holder, minute)
        self.assertEqual(holder._push_sent, [],
                         'operator sudah merespons -> tidak ada pengingat')


class LiveThrottleTests(unittest.TestCase):
    def test_alert_event_throttled_two_seconds(self):
        bus = EventBus()
        queue = Queue()
        bus.subscribe(queue, role='hse')
        payload = {'equipmentInstanceId': 'u1', 'kind': 'person_entered_zone'}
        for _ in range(3):
            bus.publish_typed('alert.created', payload,
                              throttle_key='alert:u1:person_entered_zone',
                              min_interval_s=2.0)
        self.assertEqual(queue.qsize(), 1, 'event alert live di-throttle')

    def test_event_without_throttle_key_stays_realtime(self):
        bus = EventBus()
        queue = Queue()
        bus.subscribe(queue, role='hse')
        for _ in range(3):
            bus.publish_typed('assignment.status', {'x': 1})
        self.assertEqual(queue.qsize(), 3, 'event lain tetap real-time')


if __name__ == '__main__':
    unittest.main()
