"""Evidence burst recorder — simpan JPEG ring-buffer sekitar momen alert.

Saat alert terbit, CameraManager membekukan ring-buffer frame terakhir
(deque berukuran tetap) lalu EvidenceRecorder menuliskannya sebagai JPEG
ke <storage_dir>/<camera_id>/<alert_id>/frame_NNN.jpg + metadata.json
(di direktori yang sama).

Retensi WAJIB di luar repository: default storage di bawah direktori
temporer sistem (<tmp>/opencode/evidence) dan hanya dipakai bila sudah ada,
atau ditimpa eksplisit lewat env EVIDENCE_DIR. Fail-open: tanpa EVIDENCE_DIR
yang jelas dan direktori default belum ada, recorder nonaktif (`enabled`
False) — pemanggil (CameraManager) skip evidence dan mencatat peringatan,
bukan crash.

cv2 diimpor LAZY (di dalam `record`) supaya modul ini tidak crash di
lingkungan tanpa requirements-ml; impor level modul murni stdlib.
"""
import json
import os
import tempfile
from pathlib import Path

_JPEG_QUALITY = 85


def _default_storage_dir():
    """Lokasi evidence default: di luar repository, di bawah direktori temporer."""
    return Path(tempfile.gettempdir()) / 'opencode' / 'evidence'


class EvidenceRecorder:
    """Tulis burst JPEG + metadata per alert; baca kembali untuk disajikan."""

    def __init__(self, storage_dir=None):
        # Prioritas: argumen eksplisit > env EVIDENCE_DIR > default temporer.
        if storage_dir is None:
            storage_dir = os.environ.get('EVIDENCE_DIR')
        self.enabled = True
        if storage_dir is None:
            default = _default_storage_dir()
            if not default.exists():
                # Fail-open: jangan buat direktori di lokasi default secara
                # otomatis — evidence nonaktif sampai operator menyiapkan path
                # atau menyetel EVIDENCE_DIR.
                self.enabled = False
                self.storage_dir = default
                return
            storage_dir = default
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    # -- tulis ---------------------------------------------------------------
    def record(self, camera_id, alert_id, frames, metadata=None):
        """Tulis burst JPEG + metadata.json; return dict ringkas atau None.

        frames: iterable (timestamp_ms, np_frame). Tidak ada frame → None
        (tidak membuat direktori). Gagal encode satu frame dilewati; file
        tetap dihitung hanya bila benar-benar tertulis.
        """
        if not self.enabled:
            return None
        frames = list(frames)
        if not frames:
            return None
        directory = self.storage_dir / str(camera_id) / str(alert_id)
        directory.mkdir(parents=True, exist_ok=True)
        import cv2  # lazy: hanya dibutuhkan saat benar-benar menulis evidence

        stamps = [ts for ts, _ in frames if ts is not None]
        span_ms = (max(stamps) - min(stamps)) if stamps else 0

        file_count = 0
        for index, (timestamp_ms, frame) in enumerate(frames):
            ok, buffer = cv2.imencode(
                '.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), _JPEG_QUALITY])
            if not ok:
                continue
            (directory / f'frame_{index:03d}.jpg').write_bytes(buffer.tobytes())
            file_count += 1

        meta = dict(metadata or {})
        meta.update(camera_id=str(camera_id), alert_id=str(alert_id),
                    span=span_ms, file_count=file_count,
                    timestampsMs=[timestamp_ms for timestamp_ms, _ in frames])
        (directory / 'metadata.json').write_text(json.dumps(meta, indent=2))

        return {'path': str(directory), 'file_count': file_count,
                'duration_span_ms': span_ms}

    # -- baca ----------------------------------------------------------------
    def list_for_alert(self, alert_id):
        """Daftar per kamera: path, file JPEG, dan metadata untuk satu alert."""
        results = []
        if not self.enabled or not self.storage_dir.is_dir():
            return results
        for camera_dir in sorted(p for p in self.storage_dir.iterdir() if p.is_dir()):
            alert_dir = camera_dir / str(alert_id)
            if not alert_dir.is_dir():
                continue
            metadata = {}
            meta_path = alert_dir / 'metadata.json'
            if meta_path.is_file():
                try:
                    metadata = json.loads(meta_path.read_text())
                except (ValueError, OSError):
                    metadata = {}
            files = sorted(str(p) for p in alert_dir.glob('frame_*.jpg'))
            results.append({'camera_id': camera_dir.name, 'alert_id': str(alert_id),
                            'path': str(alert_dir), 'files': files,
                            'metadata': metadata})
        return results

    def serve(self, alert_id):
        """(filename, bytes) untuk tiap frame burest — siap dikirim handler HTTP."""
        pairs = []
        for entry in self.list_for_alert(alert_id):
            directory = Path(entry['path'])
            for path in sorted(directory.glob('frame_*.jpg')):
                pairs.append((path.name, path.read_bytes()))
        return pairs


def get_evidence_files(alert_id, storage_dir=None):
    """Convenience untuk lapisan HTTP (wiring endpoint menyusul integrasi).

    Return struktur list_for_alert; tanpa storage_dir memakai env/default.
    """
    return EvidenceRecorder(storage_dir=storage_dir).list_for_alert(alert_id)
