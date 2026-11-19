"""Command-line configuration for the local server."""
import argparse
import math
import os
from pathlib import Path

from .core.hazard import CLEAR_HOLD_S
from .core.zones import DEFAULT_ZONE_RADIUS_M

SERVER_ROOT = Path(__file__).resolve().parents[1]  # AppleApp/Server
ENV_FILE = SERVER_ROOT / '.env'

# MUST run at module import — the earliest common ancestor of every entrypoint
# (main, Tools, tests), BEFORE any lazy `import cv2` elsewhere: OpenCV reads
# this env one last time when its FFmpeg backend first initializes (first
# VideoCapture/import), so a later setdefault is too late and RTSP stays on
# its broken-for-us default UDP (packet loss → corrupt h264 → decode spam →
# SIGSEGV in the decoder thread; field logs "RTP bad cseq" 14–15 Nov, watch
# run #1 exit 139). TCP interleaved is lossless on loopback/LAN and supported
# by effectively all IP cameras. Keep before ANY Sources import so ordering
# can never regress.
os.environ.setdefault('OPENCV_FFMPEG_CAPTURE_OPTIONS', 'rtsp_transport;tcp')


def env_values(key):
    """Return all values recorded for key: process environment, else .env lines (unquoted)."""
    value = os.environ.get(key)
    if value is not None:
        return [value]
    if ENV_FILE.exists():
        return [line.partition('=')[2].strip() for line in ENV_FILE.read_text().splitlines()
                if line.startswith(key + '=')]
    return []


def env_value(key, default=None):
    """Read key from process environment, falling back to Server/.env (one unquoted line per key)."""
    values = env_values(key)
    return values[0] if values else default


def _positive_float(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f'harus berupa angka: {text!r}')
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f'harus angka positif (> 0): {text!r}')
    return value


def _zone_radius_default(parser):
    # Env ZONE_RADIUS_M dipakai sebagai default --zone-radius; nilai rusak
    # gagal rapi lewat parser.error (usage + exit 2), bukan traceback.
    raw = env_value('ZONE_RADIUS_M')
    if not raw:
        return DEFAULT_ZONE_RADIUS_M
    try:
        return _positive_float(raw)
    except argparse.ArgumentTypeError as error:
        parser.error(f'ZONE_RADIUS_M tidak valid: {error}')


def _non_negative_float(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f'harus berupa angka: {text!r}')
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f'harus angka >= 0: {text!r}')
    return value


def _clear_hold_default(parser):
    raw = env_value('CLEAR_HOLD_S')
    if not raw:
        return CLEAR_HOLD_S
    try:
        return _non_negative_float(raw)
    except argparse.ArgumentTypeError as error:
        parser.error(f'CLEAR_HOLD_S tidak valid: {error}')


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; loopback by default for local HTTP development")
    parser.add_argument("--video", default=None, help="Video rekaman diputar sebagai sumber frame (input awal produk, bukan CCTV)")
    parser.add_argument("--video-fps", type=float, default=None, help="Sampling frame per detik saat memutar --video; default semua frame")
    parser.add_argument("--video-loop", action="store_true", help="Ulangi video dari awal setelah selesai")
    parser.add_argument("--auto-start-cameras", action="store_true",
                        help="Nyalakan semua kamera recording terdaftar (POST/Sources.cameras.cli) saat boot")
    parser.add_argument("--zone-radius", type=_positive_float,
                        default=_zone_radius_default(parser),
                        help="Radius zona bahaya excavator dalam meter (default 4; env ZONE_RADIUS_M; diabaikan tanpa --calib)")
    parser.add_argument("--clear-hold", type=_non_negative_float,
                        default=_clear_hold_default(parser),
                        help="Detik zona harus kosong berturut-turut sebelum bahaya dinyatakan selesai "
                             "(default 3; env CLEAR_HOLD_S; 0 = tanpa jeda)")
    parser.add_argument("--calib", default=None,
                        help="File kalibrasi ground-plane .calib.json hasil GroundCalibrationV2; zona meter aktif; tanpa ini zona fallback piksel")
    return parser.parse_args()
