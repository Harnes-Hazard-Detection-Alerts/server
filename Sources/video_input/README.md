# Input video

Sumber awal adalah video rekaman, bukan CCTV fisik. Pembaca video sudah terpasang:
`reader.py` (`RecordingReader`, sampling fps via stride) dan `service.py`
(thread playback yang memanggil `AlertServer.process_frame` per frame dengan pacing realtime).
`RTSPReader` menangani CCTV live: reconnect maksimal 3× open gagal beruntun
(±6 detik) lalu scan berhenti — kamera masuk status `error` dengan healthNote
`fallback` (dikontrol `playback.py`); pemulihan lewat start ulang.

Jalankan: `python3 -m Sources.main --video <path.mp4> [--video-fps 8] [--video-loop]`.
Simpan rekaman di luar repository.
