"""Pembaca video menjadi stream frame (video_input).

Dua sumber: rekaman (file di Mac) dan RTSP (CCTV live — iPhone via Continuity
+ ffmpeg produser, atau kamera IP komersial). Keduanya menghasilkan antarmuka
sama: (index, ms_sejak_buka, frame) supaya pipeline playback tak peduli asal.
"""
import time
from pathlib import Path


class RecordingReader:
    """Buka video rekaman dan menghasilkan (index, timestamp_ms, frame) per frame terpilih.

    target_fps memangkas laju frame dengan stride bulat (frame ke-N dari sumber),
    bukan decode parsial; timestamp memakai jam container video, bukan jam dinding.
    """

    def __init__(self, path, target_fps=None):
        import cv2  # lazy: modul inti server berjalan tanpa requirements-ml.txt
        self.path = str(path)
        if not Path(self.path).exists():
            raise FileNotFoundError(f'video tidak ditemukan: {self.path}')
        self.cap = cv2.VideoCapture(self.path)
        if not self.cap.isOpened():
            raise ValueError(f'video tidak bisa dibuka: {self.path}')
        self.fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0.0) or 30.0
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if target_fps:
            self.interval = max(1, round(self.fps / float(target_fps)))
        else:
            self.interval = 1

    @property
    def effective_fps(self):
        return self.fps / self.interval

    def frames(self):
        read_index = 0
        out_index = 0
        while True:
            ok, frame = self.cap.read()
            if not ok:
                break
            if read_index % self.interval == 0:
                yield out_index, round(read_index * 1000 / self.fps), frame
                out_index += 1
            read_index += 1

    def release(self):
        self.cap.release()


class RTSPReader:
    """Buka stream RTSP (CCTV live) dan hasilkan (index, ms_sejak_buka, frame).

    Sama seperti RecordingReader: timestamp berjalan dari pembukaan, pipeline
    playback menerjemahkannya ke Unix-epoch via anchor sama. Stream putus →
    tunggu reconnect_delay lalu coba lagi; reconnect hanya mengulang momen open —
    produser RTSP yang mati (DESCRIBE 404 → isOpened False) tidak pernah
    masuk fase baca. Budget max_failed_opens open-gagal BERUNTUN mengakhiri
    generator: tidak ada scan abadi — kamera RTSP produsennya mati sampai
    ±6 detik lalu fallback ke manager (status error; pemulihan via start
    ulang). Sukses open mereset hitungan budget. Generator juga berhenti
    bila release() dipanggil (stop playback/camera). target_fps menjadi
    stride (frame ke-N) — penjaga CPU/MPS bila sumber rapat.
    """

    def __init__(self, url, target_fps=None, reconnect_delay=2.0, on_state=None,
                 max_failed_opens=3, cv2_module=None, capture_factory=None):
        if cv2_module is None:
            import cv2 as cv2_module  # lazy: modul inti server berjalan tanpa requirements-ml.txt
        self.cv2 = cv2_module
        self.url = str(url)
        if not self.url.startswith('rtsp://'):
            raise ValueError(f'URL RTSP diharapkan, dapat: {self.url}')
        # Transport ditetapkan di Sources/config saat module-load (sebelum
        # `import cv2` pertama di mana pun — OpenCV membaca env ini hanya saat
        # backend FFmpeg pertama kali dibentuk; set di sini terbukti terlambat:
        # kamera file membuka cv2 lebih dulu, lalu RTSP tetap UDP → paket rusak
        # → SEGV dekoder, kejadian 14-15 Nov "RTP bad cseq", run #1 exit 139).
        self.open_capture = capture_factory or cv2_module.VideoCapture
        self.target_fps = float(target_fps) if target_fps else None
        self.reconnect_delay = reconnect_delay
        # 3 open gagal beruntun = produser benar-benar mati → berhenti mencari
        # (keputusan owner 12 Nov 2026); kamera hidup kembali lewat start ulang.
        self.max_failed_opens = max(1, int(max_failed_opens))
        self.on_state = on_state  # callable('live'|'stale') — status kamera
        self.released = False
        self.fps = 0.0
        self.width = 0
        self.height = 0
        self._cap = None

    @property
    def effective_fps(self):
        """Laju yang dihasilkan: min(laju sumber, target_fps) bila diset."""
        if self.target_fps:
            return min(self.fps or self.target_fps, self.target_fps)
        return self.fps or 0.0

    def frames(self):
        out_index = 0
        opened_at = time.perf_counter()
        failed_opens = 0
        while not self.released:
            cap = self.open_capture(self.url)
            if not cap.isOpened():
                cap.release()
                if self.released:
                    return
                failed_opens += 1
                if failed_opens >= self.max_failed_opens:
                    if self.on_state:
                        self.on_state('stale')  # budget habis → fallback manager
                    return  # scan berhenti — jangan mencari selamanya
                time.sleep(self.reconnect_delay)  # produser belum hidup — tunggu
                continue
            self._cap = cap
            if failed_opens:
                failed_opens = 0
                if self.on_state:
                    self.on_state('live')  # sembuh → kembali online
            self.fps = float(cap.get(self.cv2.CAP_PROP_FPS) or 0.0)
            self.width = int(cap.get(self.cv2.CAP_PROP_FRAME_WIDTH))
            self.height = int(cap.get(self.cv2.CAP_PROP_FRAME_HEIGHT))
            source_fps = self.fps if self.fps > 0 else self.target_fps
            interval = 1
            if self.target_fps and source_fps:
                interval = max(1, round(source_fps / self.target_fps))
            read_index = 0
            try:
                while not self.released:
                    ok, frame = cap.read()
                    if not ok:  # stream putus / redeo EOF — reconnect loop luar
                        break
                    if read_index % interval == 0:
                        yield (out_index,
                               round((time.perf_counter() - opened_at) * 1000),
                               frame)
                        out_index += 1
                    read_index += 1
            finally:
                cap.release()
                self._cap = None
            if self.released:
                return
            time.sleep(self.reconnect_delay)

    def release(self):
        self.released = True
        if self._cap is not None:
            self._cap.release()
