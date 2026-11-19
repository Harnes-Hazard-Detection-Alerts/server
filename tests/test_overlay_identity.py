"""Overlay radius ikut slider — seam identitas sampai render (14 Nov 2026).

Kasus Depo (laporan owner): slider Safety Zone set 2 m, orang 2.8 m tetap
digambar garis merah + elips 4 m — evaluator sudah benar (alert diam),
tapi overlay memakai identitas prefix kamera (kuno) sehingga setelan
radius per-unit tak pernah ketemu. Test: _zone_radius_m WAJIB membaca
identitas unit ter-DAFTAR via resolver yang diinjeksi.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Sources.cameras.playback import CameraManager


class _Settings:
    """Stub zone_settings: radius per identitas (duck-typed radius_for)."""

    def __init__(self, table):
        self._table = table

    def radius_for(self, identity):
        return self._table.get(identity)


class _Context:
    def __init__(self, settings):
        self.zone_settings = settings


class _Server:
    def __init__(self, context):
        self.context = context


def _manager_with(settings, resolver=None):
    manager = CameraManager()
    manager._server = _Server(_Context(settings))
    if resolver is not None:
        manager.unit_identity_for = resolver
    return manager


class ZoneRadiusOverlayIdentityTests(unittest.TestCase):
    def test_render_radius_mengikuti_slider_unit_terdaftar(self):
        """Slider set 2 m di unit hasil relink → render elips/garis pakai 2,
        bukan default 4 (semua konsumen radius render lewat fungsi yang sama)."""
        settings = _Settings({'726b111b-eq-1': 2})     # unit ter-DAFTAR
        manager = _manager_with(settings,
                                resolver=lambda camera_id: '726b111b-eq-1')
        radius = manager._zone_radius_m('f0cd2f10-9999-relink')  # kamera RTSP
        self.assertEqual(radius, 2.0,
                         'overlay wajib membaca identitas unit ter-daftar')

    def test_render_radius_fallback_prefix_tanpa_resolver(self):
        """Regresi: tanpa injector, perilaku lama (prefix kamera) utuh."""
        settings = _Settings({'f0cd2f10-eq-1': 3})
        manager = _manager_with(settings)                # resolver tidak disuntik
        radius = manager._zone_radius_m('f0cd2f10-9999-cam')
        self.assertEqual(radius, 3.0,
                         'tanpa resolver, identitas prefix kamera tetap dipakai')

    def test_render_radius_resolver_miss_kembali_prefix(self):
        """Kamera tanpa link registry → resolver None → fallback prefix."""
        settings = _Settings({'f0cd2f10-eq-1': 5})
        manager = _manager_with(settings, resolver=lambda camera_id: None)
        radius = manager._zone_radius_m('f0cd2f10-9999-cam')
        self.assertEqual(radius, 5.0,
                         'resolver miss harus jatuh ke prefix, bukan error')


if __name__ == '__main__':
    unittest.main()
