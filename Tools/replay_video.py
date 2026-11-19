#!/usr/bin/env python3
"""Replay video rekaman lewat DetectionModel — ringkasa deteksi di CLI + opsi video beranotasi.

Tanpa server HTTP/DB: untuk mengecek model + zona placeholder sebelum integrasi app.
Output per frame: baris status (EXC/ARM/PSN + confidence); ALERT ditandai baris
sendiri. --annotate menulis video beranotasi (di-downscale ke 1280px lebar).

Usage:
  .venv/bin/python Tools/replay_video.py <video.mp4> [--fps 10] [--annotate out.mp4] [--arm]
                                       [--calib file.calib.json] [--zone-radius 4]
"""
import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Sources.core.detection import (
    CLASS_EXCAVATOR,
    CLASS_EXCAVATOR_ARM,
    CLASS_PERSON,
    CLASS_NAMES,
)
from Sources.core.evaluator import RiskEvaluator
from Sources.core.zones import DEFAULT_ZONE_RADIUS_M
from Sources.detection.model import DetectionModel
from Sources.video_input.reader import RecordingReader

COLORS = {CLASS_EXCAVATOR: (0, 160, 255), CLASS_EXCAVATOR_ARM: (200, 120, 255), CLASS_PERSON: (60, 200, 60)}  # BGR


def _positive_radius(value):
    number = float(value)
    if not math.isfinite(number) or not number > 0:
        raise argparse.ArgumentTypeError(f'radius harus angka > 0: {value!r}')
    return number


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video')
    ap.add_argument('--fps', type=float, default=None, help='sampling frame per detik')
    ap.add_argument('--annotate', default=None, help='tulis video beranotasi ke path ini')
    ap.add_argument('--arm', action='store_true', help='sertakan kelas excavator_arm')
    ap.add_argument('--calib', default=None,
                    help='file kalibrasi .calib.json — zona meter aktif (default fallback piksel)')
    ap.add_argument('--zone-radius', type=_positive_radius, default=DEFAULT_ZONE_RADIUS_M,
                    help=f'radius zona dalam meter (default {DEFAULT_ZONE_RADIUS_M:g} m; butuh --calib)')
    args = ap.parse_args()

    model = DetectionModel(classes=(CLASS_EXCAVATOR, CLASS_EXCAVATOR_ARM, CLASS_PERSON) if args.arm else None)
    if model.runtime is None:
        print('model belum aktif — cek DETECTION_MODEL_PATH dan requirements-ml.txt.')
        return 1
    calibration = None
    if args.calib:
        try:
            from Sources.core.calibration import CalibrationError, load_healthy
            calibration = load_healthy(args.calib)
        except ImportError as error:
            print(f'Kalibrasi {args.calib} butuh numpy — pasang requirements-ml.txt ({error})', file=sys.stderr)
            return 1
        except OSError as error:
            print(f'File kalibrasi {args.calib} tidak terbaca: {error}', file=sys.stderr)
            return 1
        except ValueError as error:
            print(f'File kalibrasi {args.calib} rusak (JSON): {error}', file=sys.stderr)
            return 1
        except CalibrationError as error:
            print(f'Kalibrasi {args.calib} tidak lolos health-check: {error}', file=sys.stderr)
            return 1
    risk = RiskEvaluator(zone_radius_m=args.zone_radius, calibration=calibration)
    print(f"[risk] zona {'meter' if calibration else 'piksel fallback'} | "
          f"radius {args.zone_radius:g} m", flush=True)

    import cv2
    reader = RecordingReader(args.video, args.fps)
    writer = None
    if args.annotate:
        scale = 1280 / reader.width
        size = (1280, max(1, round(reader.height * scale)))
        writer = cv2.VideoWriter(args.annotate, cv2.VideoWriter_fourcc(*'mp4v'),
                                 reader.effective_fps, size)

    # Total per classId (identitas stabil); nama hanya untuk tampilan akhir.
    totals = {class_id: 0 for class_id in CLASS_NAMES}
    alerts = 0
    count = 0
    try:
        for index, ts_ms, frame in reader.frames():
            detections = model.detect(frame)
            alert = risk.evaluate(detections, timestamp_ms=ts_ms, source_id=Path(args.video).stem)
            for d in detections:
                totals[d['classId']] += 1
            tags = ' '.join(f"{d['label'].upper()}:{d['confidence']:.2f}" for d in detections)
            print(f'f{index:04d} {ts_ms / 1000:7.1f}s | {tags or "-"}', flush=True)
            if alert:
                alerts += 1
                print(f'         >>> [ALERT {alert["kind"]}] {alert["message"]}', flush=True)
            if writer is not None:
                overlay = frame
                for d in detections:
                    x1, y1, x2, y2 = d['box']
                    cv2.rectangle(overlay, (x1, y1), (x2, y2), COLORS[d['classId']],
                                  max(2, reader.width // 900))
                    cv2.putText(overlay, f"{d['label']} {d['confidence']:.2f}",
                                (x1, max(0, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                                max(0.6, reader.width / 1400), COLORS[d['classId']], 2, cv2.LINE_AA)
                writer.write(cv2.resize(overlay, size))
            count += 1
    finally:
        reader.release()
        if writer is not None:
            writer.release()

    names = ', '.join(f'{CLASS_NAMES[cid]} {n}' for cid, n in totals.items())
    print(f'\nselesai: {count} frame | total deteksi {names} | alert {alerts}')
    if args.annotate:
        print(f'video beranotasi: {args.annotate}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
