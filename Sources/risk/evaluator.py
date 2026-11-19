"""API publik evaluator risiko; implementasi dipindah ke Sources/core/evaluator.

Kontrak modul (dipertahankan): evaluate_risk(detections, timestamp_ms, source_id,
now_ms) -> dict | None, reset_state(), dan konstanta PADDING_RATIO /
MIN_ALERT_GAP_MS. Konsumen API lama: Tools/replay_video.py, tests/test_risk.py,
dan shim AppleApp/Server/detection.py. AlertServer tidak lagi memakai modul ini
— ia menyuntik Sources.core.evaluator.RiskEvaluator langsung (M2c3).
Catatan M3c2: instance default ini SELALU zona piksel — radius/kalibrasi hanya
bisa lewat RiskEvaluator(zone_radius_m=..., calibration=...) langsung.
"""
from ..core.evaluator import MIN_ALERT_GAP_MS, PADDING_RATIO, RiskEvaluator  # noqa: F401

_default = RiskEvaluator()


def evaluate_risk(detections, timestamp_ms=None, source_id=None, now_ms=None):
    """Delegator ke instance default RiskEvaluator — perilaku identik dengan celah global lama."""
    return _default.evaluate(detections, timestamp_ms=timestamp_ms,
                             source_id=source_id, now_ms=now_ms)


def reset_state():
    """Kosongkan state instance default (dipakai pengujian)."""
    _default.reset()
