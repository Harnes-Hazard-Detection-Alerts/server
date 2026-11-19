"""Konteks aplikasi — satu objek yang dilihat lapisan HTTP (M5).

Handler menjangkau fitur HANYA lewat `self.server.context.*` — berhenti
menjamah atribut server satu per satu (kontrak struktural eksplisit,
ganti threading rahasia di test_server lama). Disusun composition root
(Sources/main.py); komponen disuntik:
- bus diketik port `Publisher` (core/ports), adapter EventBus dari main;
- connect = pabrik koneksi database (DI ala AuthService.connection_factory)
  — SEMUA panggilan service dari handler menyuntik connect ini, tidak pernah
  memakai default modul (default Production mematikan pengujian dan bisa
  salah-target).
"""
from dataclasses import dataclass, field
from typing import Callable

from ..auth.limits import LoginLimits
from ..auth.service import AuthService
from ..calls.service import CallService
from ..cameras.playback import CameraManager
from ..core.evaluator import RiskEvaluator
from ..core.ports import AlertHistory, Detector, Publisher
from ..equipment.zone import ZoneSettings
from ..equipment.operation import OperationStatus


@dataclass
class AppContext:
    """Rakitan dependensi yang dipakai pipeline dan lapisan HTTP."""

    auth: AuthService
    login_limits: LoginLimits
    risk: RiskEvaluator
    detector: Detector | None
    bus: Publisher
    connect: Callable
    store: AlertHistory | None = None  # None = riwayat nonaktif (fail-open)
    enable_test_alerts: bool = False
    cameras: CameraManager = field(default_factory=CameraManager)
    zone_settings: ZoneSettings | None = None  # None = radius per unit nonaktif
    operation_status: OperationStatus | None = None
    calls: CallService | None = None  # None = Call Cabin VoIP nonaktif
