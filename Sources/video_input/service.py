"""Memutar video rekaman ke AlertServer.process_frame (video_input).

Thread daemon dengan pacing realtime: frame diproses mengikuti jam sumber
video, lalu status tiap frame ditampilkan langsung di terminal (CLI, tanpa UI)
— mewakili tampilan yang nanti diterima app sesudah integrasi.
"""
import sys
import threading
import time
from pathlib import Path

from ..core.detection import CLASS_EXCAVATOR, CLASS_EXCAVATOR_ARM, CLASS_PERSON

CLEAR_LINE = '\033[2K\r'
COLOR_EXC = '\033[33m'   # kuning
COLOR_ARM = '\033[35m'   # magenta
COLOR_PSN = '\033[32m'   # hijau
COLOR_ERR = '\033[31m'   # merah
RESET = '\033[0m'


def start_video_thread(server, video_path, target_fps=None, loop=False, quiet=False):
    source = str(Path(video_path).resolve())
    with server.context.connect() as connection:
        camera = connection.execute(
            'SELECT id FROM cameras WHERE source=%s', (source,)).fetchone()
    if camera is None:
        raise ValueError(
            '--video harus memakai source yang sudah terdaftar di tabel cameras')
    camera_id = str(camera[0])
    thread = threading.Thread(
        target=_play,
        args=(server, video_path, camera_id, target_fps, loop, quiet),
        daemon=True, name='video-input',
    )
    thread.start()
    return thread


def _play(server, video_path, camera_id, target_fps, loop, quiet):
    from .reader import RecordingReader
    stem = Path(video_path).stem
    while True:
        reader = RecordingReader(video_path, target_fps)
        print(f'[video] mulai: {stem} | {reader.width}x{reader.height} @ {reader.fps:.1f} fps '
              f'(diproses {reader.effective_fps:.1f} fps | total {reader.total_frames} frame)', flush=True)
        # Anchor epoch: timestamp container video (ms) diterjemahkan ke Unix-epoch
        # supaya kontrak *AtMs (api-draft 2) terpenuhi; tiap putaran/loop memulai
        # sesi baru (jam video kembali dari 0 pada jam dinding saat itu).
        epoch0 = time.time_ns() // 1_000_000
        started = time.perf_counter()
        count = 0
        try:
            for index, ts_ms, frame in reader.frames():
                detections, alert = server.process_frame(
                    frame, timestamp_ms=epoch0 + ts_ms, source_id=camera_id)
                if not quiet:
                    _print_status(index, ts_ms, detections, alert)
                delay = (count + 1) / reader.effective_fps - (time.perf_counter() - started)
                if delay > 0:
                    time.sleep(delay)
                count += 1
        finally:
            reader.release()
        print(f'{CLEAR_LINE}[video] selesai: {stem} ({count} frame diproses)', flush=True)
        if not loop:
            break


def _print_status(index, ts_ms, detections, alert):
    # Hitung via classId (kontrak core/detection), bukan string label.
    excavators = sum(1 for d in detections if d['classId'] == CLASS_EXCAVATOR)
    arms = sum(1 for d in detections if d['classId'] == CLASS_EXCAVATOR_ARM)
    persons = [d for d in detections if d['classId'] == CLASS_PERSON]
    swim = ' '.join(f'{d["confidence"]:.2f}' for d in persons)[:40]
    line = (f'f{index:04d} {ts_ms / 1000:7.1f}s | '
            f'{COLOR_EXC}EXC {excavators}{RESET} | {COLOR_ARM}ARM {arms}{RESET} | '
            f'{COLOR_PSN}PSN {len(persons)}{RESET} [{swim}]')
    print(CLEAR_LINE + line, end='', flush=True)
    if alert:
        color = COLOR_PSN if alert.get('kind') == 'zone_cleared' else COLOR_ERR
        print(f'{CLEAR_LINE}\n{color}[ALERT {alert["kind"]}] {alert["message"]}'
              f'{RESET} (f{index}, {ts_ms / 1000:.1f}s)', flush=True)
