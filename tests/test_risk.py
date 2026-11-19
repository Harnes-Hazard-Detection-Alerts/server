"""Perilaku evaluator zona bahaya — histeresis ganda.

Time histeresis (core/hazard, port workspace fix 7 Nov): konfirmasi butuh
kehadiran >= 0.25 s, clear butuh ketidakhadiran >= 0.5 s dalam window 1 s;
SATU FRAME tunggal tidak pernah mengubah status (anti instant-CONFIRM).
Distance histeresis (mode meter): konfirmasi mulai radius confirm (4 m),
zona aktif bertahan sampai keluar radius clear (6 m).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.risk.evaluator import PADDING_RATIO, evaluate_risk, reset_state


def detection(label, box):
    return {'label': label, 'classId': {'excavator': 0, 'excavator_arm': 1, 'person': 2}[label],
            'confidence': 0.9, 'box': box}


EXCAVATOR = detection('excavator', (100, 100, 500, 300))
NEAR_PERSON = detection('person', (400, 250, 420, 290))    # tengah (410, 270) di dalam padding
FAR_PERSON = detection('person', (700, 500, 720, 540))     # tengah (710, 520) jauh dari box
SECOND_PERSON = detection('person', (420, 250, 440, 290))  # tengah (430, 270) juga di dalam padding
ARM = detection('excavator_arm', (150, 120, 200, 280))


class RiskTimeHysteresisTests(unittest.TestCase):
    def setUp(self):
        reset_state()

    def feed(self, detections, now_ms, source='v', ts=1000):
        return evaluate_risk(detections, timestamp_ms=ts, source_id=source, now_ms=now_ms)

    def test_single_frame_never_confirms(self):
        # Anti-regresi bug 7 Nov (instant-CONFIRM): satu frame tunggal bukan bukti.
        self.assertIsNone(self.feed([EXCAVATOR, NEAR_PERSON], 10_000))

    def test_sustained_presence_confirms(self):
        self.assertIsNone(self.feed([EXCAVATOR, NEAR_PERSON], 10_000))
        entered = self.feed([EXCAVATOR, NEAR_PERSON], 10_300)      # dt = 0.3 s >= 0.25 s
        self.assertEqual(entered['kind'], 'person_entered_zone')
        self.assertEqual(entered['message'], 'Person terdeteksi di zona excavator')
        self.assertEqual(entered['personsInZone'], 1)
        self.assertEqual(entered['sourceId'], 'v')
        self.assertEqual(entered['frameTimestampMs'], 1000)
        # keadaan sama setelah aktif = bukan transisi
        self.assertIsNone(self.feed([EXCAVATOR, NEAR_PERSON], 10_400))

    def test_absence_shorter_than_clear_time_keeps_zone_active(self):
        self.feed([EXCAVATOR, NEAR_PERSON], 10_000)
        self.feed([EXCAVATOR, NEAR_PERSON], 10_300)               # konfirmasi
        self.assertIsNone(self.feed([EXCAVATOR, FAR_PERSON, ARM], 10_600))  # keluar 0.3 s
        self.assertIsNone(self.feed([EXCAVATOR, FAR_PERSON, ARM], 10_900))  # kumulatif 0.3+0.3 di window
        cleared = self.feed([EXCAVATOR, FAR_PERSON, ARM], 11_200)           # >= 0.5 s luar
        self.assertEqual(cleared['kind'], 'zone_cleared')
        self.assertEqual(cleared['personsInZone'], 0)

    def test_count_change_alerts_after_cooldown(self):
        # Kebijakan 12 Nov: perubahan jumlah butuh STABIL 1 s (count_stable_s)
        # + jeda min_alert_gap_ms (kini khusus count) sejak alert terakhir unit.
        self.feed([EXCAVATOR, NEAR_PERSON], 10_000)
        entered = self.feed([EXCAVATOR, NEAR_PERSON], 10_300)
        self.assertEqual(entered['kind'], 'person_entered_zone')   # last=10_300
        self.assertIsNone(self.feed([EXCAVATOR, NEAR_PERSON, SECOND_PERSON], 10_400))  # count baru dilihat → stabil-timer
        self.assertIsNone(self.feed([EXCAVATOR, NEAR_PERSON, SECOND_PERSON], 11_000))  # stabil 0.6 s < 1 s
        self.assertIsNone(self.feed([EXCAVATOR, NEAR_PERSON, SECOND_PERSON], 12_200))  # stabil ok, jeda 1.9 s < 2 s
        changed = self.feed([EXCAVATOR, NEAR_PERSON, SECOND_PERSON], 12_500)
        self.assertEqual(changed['kind'], 'person_count_changed')  # 12_500-10_300 >= 2 s
        self.assertEqual(changed['personsInZone'], 2)

    def test_reentry_alert_fires_immediately_no_swallow(self):
        # Kebijakan 12 Nov (fast alert): entry/high-risk TERBIT LANGSUNG pada
        # transisi dan cleared SELALU terbit — konfirmasi tidak pernah ditahan
        # (dulu: pending + cleared jeda menelan cycle <2 s = false negative).
        self.feed([EXCAVATOR, NEAR_PERSON], 10_000)
        entered = self.feed([EXCAVATOR, NEAR_PERSON], 10_300)      # konfirmasi (dt 0.3 s)
        self.assertEqual(entered['kind'], 'person_entered_zone')   # last=10_300
        # keluar >= 0.5 s -> cleared
        self.assertIsNone(self.feed([EXCAVATOR, FAR_PERSON, ARM], 10_600))
        self.assertIsNone(self.feed([EXCAVATOR, FAR_PERSON, ARM], 10_900))
        cleared = self.feed([EXCAVATOR, FAR_PERSON, ARM], 11_200)
        self.assertEqual(cleared['kind'], 'zone_cleared')          # last=11_200
        # re-entry cepat HANYA 0.9 s setelah cleared: konfirmasi 2 frame
        # (11_800 + 12_100) langsung terbit — TIDAK ditahan, TIDAK pending.
        self.assertIsNone(self.feed([EXCAVATOR, FAR_PERSON, ARM], 11_500))
        self.assertIsNone(self.feed([EXCAVATOR, NEAR_PERSON], 11_800))
        reentered = self.feed([EXCAVATOR, NEAR_PERSON], 12_100)
        self.assertEqual(reentered['kind'], 'person_entered_zone')  # FO fast alert
        self.assertEqual(reentered['personsInZone'], 1)
        # keluar lagi: cleared TETAP terbit — cycle tak pernah tertelan.
        self.assertIsNone(self.feed([EXCAVATOR, FAR_PERSON, ARM], 12_400))
        self.assertIsNone(self.feed([EXCAVATOR, FAR_PERSON, ARM], 12_700))
        cleared_again = self.feed([EXCAVATOR, FAR_PERSON, ARM], 13_000)
        self.assertEqual(cleared_again['kind'], 'zone_cleared')
        # dan masuk lagi tetap terbit normal.
        self.assertIsNone(self.feed([EXCAVATOR, NEAR_PERSON], 13_300))
        entered_third = self.feed([EXCAVATOR, NEAR_PERSON], 13_600)
        self.assertEqual(entered_third['kind'], 'person_entered_zone')

    def test_no_excavator_never_activates(self):
        self.assertIsNone(self.feed([NEAR_PERSON], 10_000))
        self.assertIsNone(self.feed([NEAR_PERSON], 10_600))

    def test_person_outside_padding_not_in_zone(self):
        edge = detection('person', (650, 400, 670, 440))  # tengah (660,420) di luar padding
        self.assertIsNone(self.feed([EXCAVATOR, edge], 10_000))
        self.assertIsNone(self.feed([EXCAVATOR, edge], 10_300))

    def test_identity_is_class_id_not_label(self):
        stranger = {'label': 'BUKAN-ORANG', 'classId': 2, 'confidence': 0.9, 'box': (400, 250, 420, 290)}
        self.assertIsNone(self.feed([EXCAVATOR, stranger], 10_000))
        entered = self.feed([EXCAVATOR, stranger], 10_300)
        self.assertEqual(entered['kind'], 'person_entered_zone')

    def test_right_label_wrong_class_id_ignored(self):
        impostor = {'label': 'person', 'classId': 5, 'confidence': 0.9, 'box': (400, 250, 420, 290)}
        self.assertIsNone(self.feed([EXCAVATOR, impostor], 10_000))
        self.assertIsNone(self.feed([EXCAVATOR, impostor], 10_300))

    def test_padding_constant_kept(self):
        # pastikan konstanta masih sesuai asumsi geometri mode piksel
        self.assertEqual(PADDING_RATIO, 0.25)


class EvaluatorInstanceTests(unittest.TestCase):
    def test_evaluator_instances_do_not_share_state(self):
        # Regresi M2: state tidak global modul — tiap instance membawa sendiri.
        from Sources.core.evaluator import RiskEvaluator
        first = RiskEvaluator()
        self.assertIsNone(first.evaluate([EXCAVATOR, NEAR_PERSON], timestamp_ms=1,
                                         source_id='v', now_ms=10_000))
        one = first.evaluate([EXCAVATOR, NEAR_PERSON], timestamp_ms=1,
                             source_id='v', now_ms=10_300)
        self.assertEqual(one['kind'], 'person_entered_zone')
        fresh = RiskEvaluator()  # state sendiri: ulang sekuens dua frame
        self.assertIsNone(fresh.evaluate([EXCAVATOR, NEAR_PERSON], timestamp_ms=1,
                                         source_id='v', now_ms=10_050))
        again = fresh.evaluate([EXCAVATOR, NEAR_PERSON], timestamp_ms=1,
                               source_id='v', now_ms=10_300)
        self.assertEqual(again['kind'], 'person_entered_zone')
        # keadaan sama pada instance yang sama = bukan transisi
        self.assertIsNone(first.evaluate([EXCAVATOR, NEAR_PERSON], timestamp_ms=1,
                                         source_id='v', now_ms=12_000))


class FakeMeterCalib:
    """Duck-typed kalibrasi: to_meter(x, y) = (x/100, y/100)."""

    def to_meter(self, x, y):
        return (x / 100, y / 100)


def person_ground_at(x_ground):
    """Deteksi person dengan titik tanah (x_ground, 300)."""
    return {'label': 'person', 'classId': 2, 'confidence': 0.9,
            'box': (x_ground - 10, 260, x_ground + 10, 300)}


METER_EXCAVATOR = {'label': 'excavator', 'classId': 0, 'confidence': 0.9,
                   'box': (100, 100, 500, 300)}     # titik tanah -> (3.0, 3.0) m


class MeterZoneEvaluatorTests(unittest.TestCase):
    def make(self, radius=4.0, clear_radius=6.0):
        from Sources.core.evaluator import RiskEvaluator
        return RiskEvaluator(zone_radius_m=radius, clear_radius_m=clear_radius,
                             calibration=FakeMeterCalib())

    def feed(self, evaluator, persons_px, now_ms, source='meter', ts=1):
        return evaluator.evaluate([METER_EXCAVATOR] + [person_ground_at(p) for p in persons_px],
                                  timestamp_ms=ts, source_id=source, now_ms=now_ms)

    def test_meter_confirms_within_four_meters(self):
        evaluator = self.make()
        self.assertIsNone(self.feed(evaluator, [690], 10_000))    # 3.9 m
        entered = self.feed(evaluator, [690], 10_300)
        self.assertEqual(entered['kind'], 'person_entered_zone')

    def test_band_between_radii_holds_active_zone_but_never_confirms(self):
        # 5.3 m: di band histeresis 4-6 m — tidak cukup mengonfirmasi zona baru...
        fresh = self.make()
        self.assertIsNone(self.feed(fresh, [830], 10_000))
        self.assertIsNone(self.feed(fresh, [830], 10_300))
        self.assertIsNone(self.feed(fresh, [830], 10_600))
        # ...tetapi mempertahankan zona yang sudah aktif (radius clear 6 m)
        active = self.make()
        self.feed(active, [690], 10_000)
        self.feed(active, [690], 10_300)                          # aktif di 3.9 m
        self.assertIsNone(self.feed(active, [830], 10_600))      # mundur ke 5.3 m — tetap aktif
        self.assertIsNone(self.feed(active, [830], 10_900))
        self.assertIsNone(self.feed(active, [1000], 11_200))     # 7.0 m — perlu akumulasi
        self.assertIsNone(self.feed(active, [1000], 11_500))
        cleared = self.feed(active, [1000], 11_800)              # >= 0.5 s luar zona
        self.assertEqual(cleared['kind'], 'zone_cleared')

    def test_zone_radius_from_user_input(self):
        # 'opsi function menerima input berapa meter' (owner 8 Nov 2026)
        tight = self.make(radius=2.0)
        self.assertIsNone(self.feed(tight, [690], 10_000))        # 3.9 m > 2 m
        self.assertIsNone(self.feed(tight, [690], 10_300))
        self.assertIsNone(self.feed(tight, [690], 10_600))

    def test_meter_count_transition(self):
        evaluator = self.make()
        self.feed(evaluator, [690], 10_000)
        self.feed(evaluator, [690], 10_300)
        self.assertIsNone(self.feed(evaluator, [690, 710], 10_400))   # jeda; satu di luar
        changed = self.feed(evaluator, [690, 490], 12_500)           # keduanya di dalam (3.9 & 1.9 m)
        self.assertEqual(changed['kind'], 'person_count_changed')
        self.assertEqual(changed['personsInZone'], 2)

    def test_invalid_radius_rejected_loudly(self):
        from Sources.core.evaluator import RiskEvaluator
        with self.assertRaises(ValueError):
            RiskEvaluator(zone_radius_m=0, calibration=FakeMeterCalib())
        with self.assertRaises(ValueError):
            RiskEvaluator(zone_radius_m=6.0, clear_radius_m=4.0, calibration=FakeMeterCalib())

NEAR_PERSON_B = detection('person', (1300, 250, 1320, 290))  # dekat excavator B


class HighRiskMeterTests(unittest.TestCase):
    """Zona high-risk ½ radius (keputusan owner 8-9 Nov 2026) — mode meter.

    METER_EXCAVATOR titik tanah (3.0, 3.0) m; person_ground_at(x) berjarak
    |x/100 - 3.0| meter. High-risk = 2 m (½ dari regulasi 4 m, default 0.5).
    """

    def make(self, ratio=0.5, radius=4.0, clear_radius=6.0):
        from Sources.core.evaluator import RiskEvaluator
        return RiskEvaluator(zone_radius_m=radius, clear_radius_m=clear_radius,
                             high_risk_ratio=ratio, calibration=FakeMeterCalib())

    def feed(self, evaluator, persons_px, now_ms, source='meter', ts=1):
        return evaluator.evaluate(
            [METER_EXCAVATOR] + [person_ground_at(p) for p in persons_px],
            timestamp_ms=ts, source_id=source, now_ms=now_ms)

    def test_person_at_1_5m_is_high_risk(self):
        # 1.5 m: dalam high-risk 2 m — person_high_risk setelah histeresis waktu.
        ev = self.make()
        self.assertIsNone(self.feed(ev, [450], 10_000))
        alert = self.feed(ev, [450], 10_300)
        self.assertEqual(alert['kind'], 'person_high_risk')
        self.assertEqual(alert['message'], 'Person terlalu dekat excavator (high risk)')
        self.assertEqual(alert['personsInZone'], 1)
        self.assertEqual(alert['sourceId'], 'meter')

    def test_person_at_3m_is_normal_entry(self):
        # 3 m: dalam regulasi 4 m, luar high-risk 2 m — person_entered_zone biasa.
        ev = self.make()
        self.assertIsNone(self.feed(ev, [600], 10_000))
        alert = self.feed(ev, [600], 10_300)
        self.assertEqual(alert['kind'], 'person_entered_zone')
        self.assertEqual(alert['personsInZone'], 1)

    def test_person_at_7m_zone_cleared(self):
        # 7 m: luar clear 6 m — single zone_cleared menutup semua level.
        ev = self.make()
        self.feed(ev, [600], 10_000)
        self.feed(ev, [600], 10_300)
        self.assertIsNone(self.feed(ev, [1000], 10_600))
        self.assertIsNone(self.feed(ev, [1000], 10_900))
        cleared = self.feed(ev, [1000], 11_200)        # >= 0.5 s luar
        self.assertEqual(cleared['kind'], 'zone_cleared')
        self.assertEqual(cleared['personsInZone'], 0)

    def test_high_risk_until_clear_is_single_zone_cleared(self):
        # Dari high-risk langsung keluar zona — TIDAK ada high_risk_cleared;
        # satu zone_cleared menutup semua level.
        ev = self.make()
        self.feed(ev, [450], 10_000)
        high = self.feed(ev, [450], 10_300)
        self.assertEqual(high['kind'], 'person_high_risk')
        self.assertIsNone(self.feed(ev, [1000], 10_600))
        self.assertIsNone(self.feed(ev, [1000], 10_900))
        cleared = self.feed(ev, [1000], 11_200)
        self.assertEqual(cleared['kind'], 'zone_cleared')

    def test_enter_normal_then_move_into_high_risk(self):
        # Masuk zona 3 m (normal), lalu geser ke 1.5 m — person_high_risk.
        ev = self.make()
        self.feed(ev, [600], 10_000)
        entered = self.feed(ev, [600], 10_300)
        self.assertEqual(entered['kind'], 'person_entered_zone')
        # jeda alert 2 s: maju melewati cooldown, lalu geser ke high-risk.
        self.assertIsNone(self.feed(ev, [600], 12_400))
        self.assertIsNone(self.feed(ev, [450], 12_600))    # high: sample pertama
        high = self.feed(ev, [450], 12_900)                # dt 0.3 s >= 0.25 s
        self.assertEqual(high['kind'], 'person_high_risk')
        self.assertEqual(high['personsInZone'], 1)

    def test_exit_high_risk_back_to_normal_no_dedicated_kind(self):
        # 1.5 m -> 3 m (masih zona regulasi): tidak ada alert khusus saat keluar
        # high-risk; zona tetap aktif tanpa kind 'high_risk_cleared'.
        ev = self.make()
        self.feed(ev, [450], 10_000)
        self.assertEqual(self.feed(ev, [450], 10_300)['kind'], 'person_high_risk')
        self.assertIsNone(self.feed(ev, [600], 10_600))
        self.assertIsNone(self.feed(ev, [600], 10_900))
        self.assertIsNone(self.feed(ev, [600], 11_200))

    def test_pixel_fallback_has_no_high_risk_kind(self):
        # Tanpa kalibrasi = mode piksel — kinds lama saja, tidak pernah
        # person_high_risk walau person bertahan lama.
        from Sources.core.evaluator import RiskEvaluator
        ev = RiskEvaluator()
        self.assertIsNone(ev.evaluate([EXCAVATOR, NEAR_PERSON], timestamp_ms=1,
                                      source_id='v', now_ms=10_000))
        entered = ev.evaluate([EXCAVATOR, NEAR_PERSON], timestamp_ms=1,
                              source_id='v', now_ms=10_300)
        self.assertEqual(entered['kind'], 'person_entered_zone')
        # sustained presence — tetap kinds lama, tidak ada high_risk
        self.assertIsNone(ev.evaluate([EXCAVATOR, NEAR_PERSON], timestamp_ms=1,
                                      source_id='v', now_ms=12_500))
        self.assertIsNone(ev.evaluate([EXCAVATOR, NEAR_PERSON], timestamp_ms=1,
                                      source_id='v', now_ms=14_500))

    def test_invalid_high_risk_ratio_rejected(self):
        from Sources.core.evaluator import RiskEvaluator
        with self.assertRaises(ValueError):
            RiskEvaluator(high_risk_ratio=1.0, calibration=FakeMeterCalib())
        with self.assertRaises(ValueError):
            RiskEvaluator(high_risk_ratio=0.0, calibration=FakeMeterCalib())


class UnitIdentityTests(unittest.TestCase):
    """equipmentInstanceId (trackId) — state terpisah per unit; tanpa trackId
    dipakai fallback identitas stabil PER SUMBER ("{prefix}-eq-1") supaya
    alert tetap bisa dirute ke app (P3: identitas bukan koordinat; adapter
    tracking nyata kelak menggantikan fallback ini)."""

    def setUp(self):
        reset_state()

    def feed(self, detections, now_ms, source='v', ts=1000):
        return evaluate_risk(detections, timestamp_ms=ts, source_id=source, now_ms=now_ms)

    def test_anonymous_alert_uses_source_scoped_fallback_id(self):
        self.assertIsNone(self.feed([EXCAVATOR, NEAR_PERSON], 10_000))
        entered = self.feed([EXCAVATOR, NEAR_PERSON], 10_300)
        self.assertEqual(entered['kind'], 'person_entered_zone')
        self.assertEqual(entered['equipmentInstanceId'], 'v-eq-1')  # prefix source: stabil

    def test_fallback_id_is_stable_per_source_and_distinct_across_sources(self):
        self.feed([EXCAVATOR, NEAR_PERSON], 10_000, source='camA')
        entered_here = self.feed([EXCAVATOR, NEAR_PERSON], 10_300, source='camA')
        reset_state()
        self.feed([EXCAVATOR, NEAR_PERSON], 10_000, source='camA')
        repeated = self.feed([EXCAVATOR, NEAR_PERSON], 10_300, source='camA')
        self.assertEqual(entered_here['equipmentInstanceId'], 'camA-eq-1')
        self.assertEqual(repeated['equipmentInstanceId'], 'camA-eq-1')  # stabil antar-proses
        self.feed([EXCAVATOR, NEAR_PERSON], 10_000, source='camB')
        entered_other = self.feed([EXCAVATOR, NEAR_PERSON], 10_600, source='camB')
        self.assertNotEqual(entered_here['equipmentInstanceId'],
                            entered_other['equipmentInstanceId'] or '')

    def test_zone_cleared_and_statuses_carry_fallback_id(self):
        from Sources.core.evaluator import RiskEvaluator
        self.feed([EXCAVATOR, NEAR_PERSON], 10_000, source='camX')
        self.feed([EXCAVATOR, NEAR_PERSON], 10_300, source='camX')
        self.assertIsNone(self.feed([EXCAVATOR], 10_600, source='camX'))
        self.assertIsNone(self.feed([EXCAVATOR], 10_900, source='camX'))
        cleared = self.feed([EXCAVATOR], 11_200, source='camX')
        self.assertEqual(cleared['kind'], 'zone_cleared')
        self.assertEqual(cleared['equipmentInstanceId'], 'camX-eq-1')
        evaluator = RiskEvaluator()
        evaluator.evaluate([EXCAVATOR, NEAR_PERSON], timestamp_ms=1000,
                           source_id='camY', now_ms=20_000)
        evaluator.evaluate([EXCAVATOR, NEAR_PERSON], timestamp_ms=1200,
                            source_id='camY', now_ms=20_300)
        statuses = evaluator.unit_statuses('camY')
        self.assertTrue(any(s['equipmentInstanceId'] == 'camY-eq-1' for s in statuses))

    def test_tracked_units_alert_separately(self):
        tracked_a = dict(EXCAVATOR, trackId='cam01-eq-1')
        tracked_b = {'label': 'excavator', 'classId': 0, 'confidence': 0.9,
                     'box': (1000, 100, 1400, 300), 'trackId': 'cam01-eq-2'}
        # hanya unit A yang aktif lebih dulu
        self.assertIsNone(self.feed([tracked_a, NEAR_PERSON], 10_000))
        first = self.feed([tracked_a, NEAR_PERSON], 10_300)
        self.assertEqual(first['equipmentInstanceId'], 'cam01-eq-1')
        # unit B baru muncul dengan person dekatnya — konfirmasi terpisah
        self.assertIsNone(self.feed([tracked_a, tracked_b, NEAR_PERSON, NEAR_PERSON_B], 10_600))
        second = self.feed([tracked_a, tracked_b, NEAR_PERSON, NEAR_PERSON_B], 10_900)
        self.assertEqual(second['equipmentInstanceId'], 'cam01-eq-2')
        self.assertEqual(second['personsInZone'], 1)     # hanya person di unit B

    def test_simultaneous_transitions_queue_fifo(self):
        tracked_a = dict(EXCAVATOR, trackId='cam01-eq-1')
        tracked_b = {'label': 'excavator', 'classId': 0, 'confidence': 0.9,
                     'box': (1000, 100, 1400, 300), 'trackId': 'cam01-eq-2'}
        both = [tracked_a, tracked_b, NEAR_PERSON, NEAR_PERSON_B]
        self.assertIsNone(self.feed(both, 10_000))
        # dua unit konfirmasi simultan -> antrean FIFO: satu alert per frame
        first = self.feed(both, 10_300)
        self.assertEqual(first['equipmentInstanceId'], 'cam01-eq-1')
        second = self.feed(both, 10_600)              # drain antrean, tanpa event baru
        self.assertEqual(second['equipmentInstanceId'], 'cam01-eq-2')
        self.assertIsNone(self.feed(both, 10_900))    # antrean kosong

    @unittest.skip("clear_hold_s = antarmuka evaluator versi remote yang belum ada di evaluator lokal — masuk via auto-merge watch; aktifkan kembali saat penyelarasan/param diimplementasikan (keputusan merge 14 Nov, local-wins server)")
    def test_excavator_vanishing_preserves_hazard_until_observation_recovers(self):
        # An unseen excavator means unknown, never proof that its zone is clear.
        from Sources.core.evaluator import RiskEvaluator
        risk = RiskEvaluator(clear_hold_s=3.0)
        def feed(detections, now_ms):
            return risk.evaluate(detections, source_id='v', now_ms=now_ms)
        feed([EXCAVATOR, NEAR_PERSON], 10_000)
        feed([EXCAVATOR, NEAR_PERSON], 10_300)
        for now_ms in range(10_600, 15_000, 300):
            self.assertIsNone(feed([NEAR_PERSON], now_ms))
        status = risk.unit_statuses('v')[0]
        self.assertEqual(status['monitoringStatus'], 'unknown')
        self.assertTrue(status['active'])
        # The clear hold starts anew once the excavator can be observed.
        for now_ms in range(15_100, 18_100, 300):
            self.assertIsNone(feed([EXCAVATOR], now_ms))
        cleared = feed([EXCAVATOR], 18_100)
        self.assertEqual(cleared['kind'], 'zone_cleared')


if __name__ == '__main__':
    unittest.main()
