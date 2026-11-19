"""Deteksi objek per frame dengan model YOLO v8 3-kelas; filter kelas di inference.

CATATAN trackId (M4): adapter ini BELUM mengisi kolom `trackId` Detection —
menyusul bersama integrasi multi-object tracking; evaluator sudah siap
memakainya sebagai kunci equipmentInstanceId per unit (core/evaluator.py).

Urutan kelas model v8 — PENTING, beda dari Core ML prototipe lama (0=person):
    0 = excavator, 1 = excavator_arm, 2 = person.
Pipeline zona-piksel MVP memakai classes=(0, 2): box arm TIDAK diteruskan
(arm = jalur upgrade zona ayun; sertakan via DETECTION_CLASSES=0,1,2 atau
argumen classes). Kalibrasi conf 0.40 / imgsz 1280 / classes [0,2] divalidasi
6 Nov 2026 di workspace model (runs/bench_sahi_v8; verifikasi visual 4 overlay)
memakai best_v8_3class.pt — yolo26n, test mAP50 0.843.

Param `classes` Ultralytics terverifikasi di ultralytics/cfg/default.yaml
("classes: filter by class id(s)") dan dipakai di engine/predictor.py; NMS
bersifat per-kelas sehingga menyaring arm tidak mengubah hasil kelas lain.

Ultralytics di-import lazy: server tetap hidup tanpa requirements-ml.txt dan
/health melaporkan modelLoaded=false (fail-open, bukan crash).

Model bawaan ikut repository di `models/best_v8_3class.pt` (5,3 MiB — kecil,
khas yolo26n). DETECTION_MODEL_PATH hanya perlu diisi untuk menimpa model itu
(versi eksperimen milik sendiri).
"""
from pathlib import Path

from ..config import SERVER_ROOT, env_value
# Kontrak identitas (Detection, classId, CLASS_NAMES) kini tinggal di
# Sources/core/detection.py (domain murni, dipakai lintas modul fitur);
# nama di-re-export di sini demi Tools dan konsumen lama.
from ..core.detection import Detection  # noqa: F401
from ..core.detection import (  # noqa: F401
    CLASS_EXCAVATOR,
    CLASS_EXCAVATOR_ARM,
    CLASS_PERSON,
    CLASS_NAMES,
)

DEFAULT_CLASSES = (CLASS_EXCAVATOR, CLASS_PERSON)  # excavator + person; arm disembunyikan untuk MVP
DEFAULT_MODEL_PATH = SERVER_ROOT / 'models' / 'best_v8_3class.pt'  # ikut repo (5.3 MiB)


class DetectionModel:
    def __init__(self, model_path=None, conf=None, imgsz=None, classes=None, device=None):
        from threading import Lock
        # Inferensi YOLO TIDAK thread-safe (state predictor dipakai-bersama);
        # multi-kamera memanggil detect() dari thread playback masing-masing.
        # Satu lock global semua deteksi di sini — evaluasi (per-source) tak dishare.
        self._infer_lock = Lock()
        env_path = env_value('DETECTION_MODEL_PATH')
        self.model_path = str(model_path or env_path or DEFAULT_MODEL_PATH)
        conf_value = conf if conf is not None else env_value('DETECTION_CONF')
        # Kebijakan 12 Nov (anti false-negative): default conf 0.35 + imgsz 1600.
        # Replay membuktikan imgsz 1280 kehilangan orang jauh/pixel-kecil di
        # frame 4K (di bawah lantai deteksi ~20 px); 1600 menempatkan orang
        # kaki-kecil di ~22 px (replay recall 3/3) dengan duty-cycle sehat
        # (~46-61% utk 4 kamera @8fps) — beda dgn 1920 yang mentok dekat 83%
        # dan menyempitkan ruang spike (kejadian 12 Nov: emit alert menggantung
        # 1.3 s di jalur playback saat 4 kamera jalan). 2560 = >100% duty —
        # menambah miss, jangan.
        self.conf = min(max(float(conf_value) if conf_value else 0.35, 0.05), 0.9)
        imgsz_value = imgsz if imgsz is not None else env_value('DETECTION_IMGSZ')
        self.imgsz = min(max(int(imgsz_value) if imgsz_value else 1600, 640), 2048)
        if classes is not None:
            self.classes = tuple(int(c) for c in classes)
        else:
            classes_value = env_value('DETECTION_CLASSES')
            self.classes = tuple(int(c) for c in classes_value.split(',')) if classes_value else DEFAULT_CLASSES
        self.device = device
        self.runtime = None
        self._load()

    def _load(self):
        if not self.model_path:
            self._warn('DETECTION_MODEL_PATH belum diisi; deteksi nonaktif.')
            return
        if not Path(self.model_path).exists():
            self._warn(f'model tidak ditemukan: {self.model_path}; deteksi nonaktif.')
            return
        try:
            from ultralytics import YOLO
        except ImportError:
            self._warn('ultralytics belum terpasang (lihat requirements-ml.txt); deteksi nonaktif.')
            return
        import torch
        device = self.device or ('mps' if torch.backends.mps.is_available() else 'cpu')
        runtime = YOLO(str(self.model_path))
        runtime.to(device)
        self.runtime = runtime
        self.device = device
        try:
            names = runtime.names
            if isinstance(names, dict) and {int(k): str(v) for k, v in names.items()} != CLASS_NAMES:
                # Identitas sudah via classId, jadi mapping names yang menyimpang
                # tidak merusak evaluasi — tapi layak diberi tahu keras.
                self._warn(f'urutan kelas model tidak cocok CLASS_NAMES repo: {names}')
        except Exception as exc:  # fail-open: metadata patologis tidak menjatuhkan server
            self._warn(f"metadata nama kelas tidak terbaca ({exc}); identitas tetap via classId")
        print(f"[detection] model {Path(self.model_path).name} siap | device {device} | "
              f"conf {self.conf} | imgsz {self.imgsz} | classes {list(self.classes)}", flush=True)

    @staticmethod
    def _warn(message):
        print(f'[detection] {message}', flush=True)

    @property
    def ready(self):
        """Kesiapan detektor (port Detector, core/ports.py) — model termuat."""
        return self.runtime is not None

    def detect(self, frame):
        """Return tuple deteksi untuk satu frame (BGR ndarray): dict per objek."""
        if self.runtime is None:
            return ()
        with self._infer_lock:  # serialisasi MPS penuh — predict DAN konversi
            try:
                result = self.runtime.predict(frame, conf=self.conf, imgsz=self.imgsz,
                                             classes=list(self.classes), device=self.device,
                                             verbose=False)[0]
            except Exception as exc:  # fail-open: frame bermasalah tidak boleh menjatuhkan server
                self._warn(f'gagal memproses frame: {exc}')
                return ()
            # Konversi WAJIB ikut dalam lock: tensor hasil masih menempel di MPS —
            # float()/int() pada box membaca GPU. Di luar lock, konversi thread-A
            # menabrak predict thread-B = SIGABRT Metal (pytorch#197805;
            # kejadian lapangan 14 Nov 2026, lihat laporan crash Thread 2/3).
            return detections_from_result(result)


def detections_from_result(result, allowed=None) -> tuple[Detection, ...]:
    """Map hasil predict Ultralytics menjadi tuple deteksi netral.

    Setiap deteksi: {"classId", "label", "confidence", "box": (x1, y1, x2, y2)}
    dalam piksel frame asli. `label` diambil dari CLASS_NAMES (kontrak repo),
    dengan fallback nama dari model bila class tak dikenal. `allowed`
    memungkinkan penyaringan kelas lanjutan (pipeline sudah menyaring lewat
    argumen classes-nya predict).
    """
    out = []
    for box in result.boxes:
        class_id = int(box.cls)
        if allowed is not None and class_id not in allowed:
            continue
        x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
        out.append({
            'classId': class_id,
            'label': CLASS_NAMES.get(class_id, str(result.names[class_id])),
            'confidence': round(float(box.conf), 3),
            'box': (round(x1), round(y1), round(x2), round(y2)),
        })
    return tuple(out)
