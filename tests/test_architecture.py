"""Kontrak arsitektur clean-lite dijaga di suite unittest biasa (tanpa tool).

Pengawal ini mencerminkan aturan CONTEXT.md 7 Nov 2026 yang SUDAH berlaku
hari ini:
- core/ = lantai domain: stdlib + typing_extensions; numpy HANYA untuk
  `core/calibration.py` (GroundCalibrationV2, pindahan M3) — per-file,
  evaluator/zones/hazard/detection tetap numpy-free.
- api/ = lapisan HTTP: boleh auth + core + users/assignments/devices
  (modul domain-API); TIDAK boleh alerts/risk/video_input/detection/
  infrastructure/config/main (rusak api->alerts M5c1 dicatat di guard).
- risk/ = delegator tipis: hanya boleh mengimpor core.
- video_input/ = adapter sumber: boleh core, bukan modul fitur lain.
Aturan antar-modul-fitur lainnya (alerts->api dll) menyusul kontrak
M4/M5; saat itu daftar ini diperluas, jangan dilonggarkan diam-diam.
Berkas .importlinter memakai aturan sama untuk grafik import penuh
(dev-only, requirements-dev.txt).
"""
import re
import sys
import unittest
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[1] / 'Sources'

# Modul Sources yang DILARANG per paket (impor = pelanggaran).
# Termasuk main (composition root) dan config (infra konfigurasi).
FORBIDDEN_SOURCES = {
    'core': ['api', 'alerts', 'auth', 'config', 'detection', 'infrastructure',
             'main', 'risk', 'users', 'video_input'],
    'risk': ['api', 'alerts', 'auth', 'config', 'detection', 'infrastructure',
             'main', 'users', 'video_input'],
    'api': ['alerts', 'config', 'detection', 'infrastructure',
            'main', 'risk', 'video_input'],
    'cameras': ['api', 'alerts', 'auth', 'config', 'detection',
                'main', 'risk', 'users'],
    'video_input': ['api', 'alerts', 'auth', 'detection', 'infrastructure',
                    'main', 'risk', 'users'],
    # calls = domain panggilan murni (stdlib): tidak tahu HTTP, bus konkret, atau DB.
    'calls': ['api', 'alerts', 'auth', 'config', 'detection', 'infrastructure',
              'main', 'risk', 'users', 'video_input'],
}
# Core: third-party whitelist. typing_extensions global (pin requirements);
# numpy HANYA untuk file kalibrasi — evaluator/zones/detection tetap murni.
CORE_GLOBAL_THIRDPARTY = {'typing_extensions'}
CORE_NUMPY_ALLOWED_FILES = {'calibration.py'}


def _targets(line):
    """Nama paket tingkat atas yang diimpor satu baris (generator).

    - 'from ..x import y' / 'from ...x import y' -> 'x'  (patch Sources)
      'from .. import x'                         -> 'x'  (patch Sources)
    - 'from .x import y'                         -> di-skip (paket sendiri)
    - 'from Sources.x import y' / 'import Sources.x' -> 'x'
    - 'from X import y' / 'import X' absolut     -> 'X'
    """
    match = re.match(r'\s*(from|import)\s+([.\w]+)', line)
    if not match:
        return
    keyword, module = match.groups()
    dots = len(module) - len(module.lstrip('.'))
    if dots:
        if dots < 2:
            return  # level 1 = dalam paket sendiri
        head = module.lstrip('.').partition('.')[0]
        if head:
            yield head
        else:  # 'from .. import a, b'
            tail = line.split(' import ', 1)
            if len(tail) == 2:
                for name in tail[1].split(','):
                    name = name.strip().partition('.')[0].strip('()')
                    if name and name.isidentifier():
                        yield name
    elif keyword == 'from':
        head = module.partition('.')[0]
        if head == 'Sources':  # absolut di dalam repo: turunkan satu segmen
            rest = module.partition('.')[2].partition('.')[0]
            if rest:
                yield rest
        else:
            yield head
    else:  # import X / import X.Y / import Sources.X
        top = module.partition('.')[0]
        if top == 'Sources':
            rest = module.partition('.')[2].partition('.')[0]
            if rest:
                yield rest
        else:
            yield top


class ArchitectureContractTests(unittest.TestCase):
    def test_packages_do_not_import_forbidden_modules(self):
        violations = []
        for package, forbidden in FORBIDDEN_SOURCES.items():
            forbidden = set(forbidden)
            for path in sorted((SOURCES / package).rglob('*.py')):
                thirdparty = set(CORE_GLOBAL_THIRDPARTY)
                if package == 'core' and path.name in CORE_NUMPY_ALLOWED_FILES:
                    thirdparty.add('numpy')
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    for target in _targets(line):
                        if target in forbidden:
                            violations.append(
                                f'{path.relative_to(SOURCES.parent)}:{number}: '
                                f'"{package}" mengimpor modul Sources "{target}"')
                        elif (package == 'core' and target != '.'
                              and target not in sys.stdlib_module_names
                              and target not in thirdparty):
                            violations.append(
                                f'{path.relative_to(SOURCES.parent)}:{number}: '
                                f'core mengimpor non-whitelist "{target}"')
        self.assertEqual(violations, [], '\n'.join(violations))

    def test_scanner_must_catch_guarded_shapes(self):
        # Kalau scanner rusak diam-diam, test ini gagal lebih dulu:
        # absolut Sources, relative dua-titik, from .. import, plain import,
        # dan level-1 harus diabaikan.
        self.assertIn('config', list(_targets('from Sources.config import env_value')))
        self.assertIn('core', list(_targets('import Sources.core')))
        self.assertIn('main', list(_targets('from ..main import build')))
        self.assertEqual({'config', 'api', 'auth'},
                         set(_targets('from .. import config, api, auth')))
        self.assertIn('cv2', list(_targets('import cv2')))
        self.assertEqual(list(_targets('from .detection import CLASS_NAMES')), [])


if __name__ == '__main__':
    unittest.main()
