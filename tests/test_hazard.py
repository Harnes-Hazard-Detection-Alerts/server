"""Unit test state machine histeresis waktu (core/hazard).

Port dari workspace fix 7 Nov 2026 — dijaga tiga jaminan struktural:
(1) satu frame tunggal tak pernah transisi; (2) dt dihitung nyata antar
sampel (zero-order hold); (3) gap besar di-clamp ke window (anti overcount).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.core.hazard import CLEARED, CONFIRMED, HazardState


class HazardStateTests(unittest.TestCase):
    def test_first_sample_has_no_interval_never_confirms(self):
        state = HazardState()
        self.assertIsNone(state.update(True, 10.0))
        self.assertEqual(state.status, 'potential')

    def test_confirm_requires_accumulated_interval(self):
        state = HazardState()
        state.update(True, 10.0)
        # dt = 0.24 s < 0.25 s — belum cukup
        self.assertIsNone(state.update(True, 10.24))
        self.assertEqual(state.status, 'potential')
        # dt tambahan 0.26 s — kumulatif window >= 0.25 s
        self.assertEqual(state.update(True, 10.50), CONFIRMED)
        self.assertEqual(state.status, 'confirmed')

    def test_zero_dt_second_sample_does_not_confirm(self):
        # Anti-regresi: dua sampel pada timestamp sama (duplikat frame) tak beri durasi.
        state = HazardState()
        state.update(True, 10.0)
        self.assertIsNone(state.update(True, 10.0))
        self.assertEqual(state.status, 'potential')

    def test_clear_requires_sustained_absence(self):
        state = HazardState()
        state.update(True, 10.0)
        state.update(True, 10.25)          # CONFIRMED
        self.assertIsNone(state.update(False, 10.55))     # interval pertama milik sampel in
        self.assertEqual(state.status, 'confirmed')
        self.assertIsNone(state.update(False, 10.85))     # kumulatif out 0.3 s < 0.5 s
        self.assertEqual(state.update(False, 11.15), CLEARED)
        self.assertEqual(state.status, 'clear')

    def test_return_inside_clear_time_resets_clear_clock(self):
        # keluar sebentar lalu balik sebelum 0.5 s — tetap confirmed.
        state = HazardState()
        state.update(True, 10.0)
        state.update(True, 10.25)
        state.update(False, 10.55)
        self.assertIsNone(state.update(True, 10.75))      # kembali dalam 0.2 s
        self.assertEqual(state.status, 'confirmed')

    def test_big_gap_is_clamped_to_window(self):
        # lompat 60 s tanpa sampel — dt di-clamp 1 s, tidak overcount lalu clear instan.
        state = HazardState()
        state.update(True, 10.0)
        state.update(True, 10.25)          # CONFIRMED
        out = state.update(False, 70.0)    # satu-satunya sampel after gap
        self.assertIsNone(out)             # belum ada interval out→out
        self.assertEqual(state.update(False, 70.5), CLEARED)

    def test_window_prunes_old_samples(self):
        state = HazardState()
        state.update(True, 0.0)
        state.update(True, 0.25)           # CONFIRMED (akumulasi 0.25)
        # masuk lagi setelah lama clear (window 1 s: sampel lama terbuang)
        state.update(False, 5.0)
        state.update(False, 5.4)           # kumulatif out 0.4 s — masih confirmed
        self.assertEqual(state.status, 'confirmed')
        state.update(False, 5.8)            # kumulatif 0.8 s >= 0.5 -> CLEARED
        self.assertEqual(state.status, 'clear')
        # in-zone baru lama terbuang — butuh akumulasi baru untuk confirm
        self.assertIsNone(state.update(True, 6.0))
        self.assertEqual(state.status, 'potential')

    def test_clear_hold_waits_for_sustained_empty_zone(self):
        # Jeda 5 s: kosong 4.5 s belum cukup, 5 s beruntun -> CLEARED.
        state = HazardState(clear_hold_s=5.0)
        state.update(True, 10.0)
        state.update(True, 10.25)                       # CONFIRMED, last in = 10.25
        for t in (10.5, 11.5, 12.5, 13.5, 14.75):
            self.assertIsNone(state.update(False, t))
        self.assertEqual(state.status, 'confirmed')
        self.assertEqual(state.update(False, 15.25), CLEARED)

    def test_clear_hold_restarts_when_worker_flickers_back(self):
        # Pekerja terlihat lagi sesaat (kedip deteksi) -> hitungan 5 s mulai ulang.
        state = HazardState(clear_hold_s=5.0)
        state.update(True, 0.0)
        state.update(True, 0.25)                        # CONFIRMED
        for t in (1.0, 2.0, 3.0, 4.0):
            self.assertIsNone(state.update(False, t))
        state.update(True, 4.25)                        # terlihat lagi sesaat
        for t in (4.5, 5.5, 6.5, 7.5, 8.5, 9.0):        # 9.0 - 4.25 = 4.75 s < 5 s
            self.assertIsNone(state.update(False, t))
        self.assertEqual(state.status, 'confirmed')
        self.assertEqual(state.update(False, 9.5), CLEARED)

    def test_clear_hold_zero_keeps_legacy_behavior(self):
        state = HazardState(clear_hold_s=0)
        state.update(True, 10.0)
        state.update(True, 10.25)
        state.update(False, 10.55)
        state.update(False, 10.85)
        self.assertEqual(state.update(False, 11.15), CLEARED)

    def test_invalid_window_rejected(self):
        with self.assertRaises(ValueError):
            HazardState(window_s=0)
        with self.assertRaises(ValueError):
            HazardState(confirm_time_s=-1)
        with self.assertRaises(ValueError):
            HazardState(clear_hold_s=-1)


if __name__ == '__main__':
    unittest.main()
