"""Import kompatibilitas lama; implementasi berada di Sources/ (detection + risk).

Kontrak shim yang dijamin: `DetectionModel` (alias Sources.detection.model.DetectionModel)
dan `evaluate_risk` (alias Sources.risk.evaluator.evaluate_risk) tetap bisa
diimpor dari sini. Nama lain tidak dijanjikan.
"""
from Sources.detection.model import DetectionModel
from Sources.risk.evaluator import evaluate_risk
