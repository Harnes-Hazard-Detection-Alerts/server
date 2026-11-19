"""Run from Server with: python3 -m Sources.main."""
import os

from .alerts.server import AlertServer
from .config import env_value, parse_args


def _load_calibration(path):
    try:
        # Lazy: numpy (requirements-ml.txt) hanya saat --calib dipakai.
        from .core.calibration import CalibrationError, load_healthy
        return load_healthy(path)
    except ImportError as error:
        raise SystemExit(f'Kalibrasi {path} butuh numpy — pasang requirements-ml.txt ({error})') from None
    except OSError as error:
        raise SystemExit(f'File kalibrasi {path} tidak terbaca: {error}') from None
    except ValueError as error:
        raise SystemExit(f'File kalibrasi {path} rusak (JSON): {error}') from None
    except CalibrationError as error:
        raise SystemExit(f'Kalibrasi {path} tidak lolos health-check: {error}') from None


def build_risk(args, radius_for=None, operating_for=None, calibration_for=None,
               identity_for=None, active_hazard_for=None, operation_started_for=None):
    # Zona meter per kamera bila ada file di Server/calibrations/; --calib
    # tetap jalan untuk kalibrasi global eksplisit. radius_for = radius per
    # excavator (slider Safety Zone app HSE). identity_for = seam identitas
    # kamera→unit ter-DAFTAR (seam 14 Nov — relink RTSP)
    from .core.evaluator import RiskEvaluator
    calibration = _load_calibration(args.calib) if args.calib else None
    mode = f'meter radius {args.zone_radius:g} m' if (calibration or calibration_for) else 'piksel (fallback tanpa kalibrasi)'
    print(f"[risk] zona {mode}", flush=True)
    # Fast alert 12 Nov: entry/high-risk/cleared terbit pada transisi (tanpa
    # jeda — kini hanya count_changed yang dibatasi min_alert_gap_ms).
    # HazardState konfirm 2 frame (0.12 s @8fps, bukan 0.15 s yang di 8fps
    # identik dengan 3 frame) + clear 0.75 s supaya tepi zona tak churn.
    # Anti-flood 16 Nov: --clear-hold (default CLEAR_HOLD_S=3 s) kini
    # DITERUSKAN ke evaluator — zona harus kosong 3 s berturut-turut sebelum
    # insiden ditutup, jadi kedip deteksi 1-2 detik tidak jadi insiden baru.
    # Knob ini sudah di-parse sejak 12 Nov tapi belum pernah sampai ke
    # RiskEvaluator (lihat core/hazard.py clear_hold_s).
    def env_float(name, default, low, high):
        try:
            value = float(env_value(name) or default)
        except (TypeError, ValueError):
            value = default
        return min(max(value, low), high)
    confirm_time_s = env_float('HARNES_CONFIRM_TIME_S', 0.12, 0.0, 1.0)
    clear_time_s = env_float('HARNES_CLEAR_TIME_S', 0.75, 0.1, 3.0)
    count_gap_ms = int(env_float('HARNES_COUNT_GAP_MS', 10_000, 0, 120_000))
    count_stable_s = env_float('HARNES_COUNT_STABLE_S', 1.0, 0.0, 5.0)
    clear_hold_s = float(args.clear_hold)
    print(f"[risk] fast alert: confirm {confirm_time_s:g}s clear {clear_time_s:g}s "
          f"clear_hold {clear_hold_s:g}s "
          f"| count_changed gap {count_gap_ms}ms stable {count_stable_s:g}s", flush=True)
    return RiskEvaluator(zone_radius_m=args.zone_radius, calibration=calibration,
                         radius_for=radius_for, operating_for=operating_for,
                         calibration_for=calibration_for, identity_for=identity_for,
                         active_hazard_for=active_hazard_for,
                         operation_started_for=operation_started_for,
                         confirm_time_s=confirm_time_s, clear_time_s=clear_time_s,
                         clear_hold_s=clear_hold_s,
                         min_alert_gap_ms=count_gap_ms, count_stable_s=count_stable_s)


def main():
    args = parse_args()
    # Composition root: adapter ultralytics disuntik lewat port Detector
    # (core/ports.py); AlertServer hanya menerima AppContext.
    from .alerts.bus import EventBus
    from .alerts.store import AlertStore
    from .api.context import AppContext
    from .auth.limits import LoginLimits
    from .auth.service import AuthService
    from .calls.service import CallService, operator_for_equipment
    from .detection.model import DetectionModel
    from .equipment.zone import ZoneSettings
    from .equipment.operation import OperationStatus
    from .equipment.registry import CameraIdentityCatalog
    from .infrastructure.database.connection import connect
    zone_settings = ZoneSettings(args.zone_radius, connection_factory=connect)
    zone_settings.load()
    operation_status = OperationStatus(connect)
    operation_status.reload()
    # Seam identitas (14 Nov 2026): kamera→unit ter-DAFTAR — relink RTSP/pendaftaran
    # ulang tidak lagi memutus pairing alert↔assignment↔radius↔fleet app.
    identity_catalog = CameraIdentityCatalog(connect)
    # Kalibrasi homografi per kamera (Server/calibrations/{Nama Kamera}.json):
    # kamera dengan file → evaluator meter + zona elips mengikuti ground;
    # sisanya tetap fallback piksel. Camera baru + file baru → restart server.
    from .cameras import service as camera_service
    from .core.calibration import load_camera_calibrations
    camera_list = camera_service.list_cameras(connect)
    ground_calibrations = load_camera_calibrations(
        os.environ.get('HARNES_CAMERA_CALIBRATIONS')
        or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'calibrations'),
        camera_list)
    calibrated = sorted(camera['name'] for camera in camera_list
                        if camera['id'] in ground_calibrations)
    if calibrated:
        print(f"[risk] kalibrasi meter kamera: {', '.join(calibrated)} "
              "(lainnya piksel fallback)", flush=True)
    # Demo defaults keep login friction low; production can enable the limit.
    limits = LoginLimits() if env_value('HARNES_LOGIN_LIMIT') == '1' else None
    bus = EventBus()
    alert_store = AlertStore(connect)
    context = AppContext(auth=AuthService(), login_limits=limits,
                         risk=build_risk(args, radius_for=zone_settings.radius_for,
                                         operating_for=operation_status.is_active,
                                         calibration_for=lambda source_id:
                                         ground_calibrations.get(source_id),
                                         identity_for=identity_catalog.equipment_for,
                                         active_hazard_for=alert_store.active_hazard_snapshot,
                                         operation_started_for=operation_status.started_at),
                         zone_settings=zone_settings,
                         operation_status=operation_status,
                         detector=DetectionModel(device=os.environ.get('DETECTION_DEVICE') or None),
                         bus=bus, connect=connect, store=alert_store,
                         calls=CallService(bus, lambda eq: operator_for_equipment(connect, eq)),
                         enable_test_alerts=env_value('ENABLE_TEST_ALERTS') == '1')
    with AlertServer((args.host, args.port), context=context) as server:
        print(f"Alert server listening on port {server.server_port}", flush=True)
        context.cameras.attach(server)
        context.cameras.ground_calibrations = ground_calibrations  # overlay zona v2
        # Overlay radius/elips/garis jarak ikut slider unit hasil relink:
        # renderer membaca identitas lewat peta yang sama dengan evaluator.
        context.cameras.unit_identity_for = identity_catalog.equipment_for
        # Kernel supervisor (owner 14 Nov 2026): intent desired_on dijamin
        # terus-menerus — kamera desired menyala sendiri saat boot, self-heal
        # saat produser telat, restart saat frame beku. Matikan eksplisit:
        # HARNES_CAMERA_SUPERVISOR=0.
        if env_value('HARNES_CAMERA_SUPERVISOR') != '0':
            context.cameras.start_supervisor()
        if args.auto_start_cameras:
            context.cameras.autostart()
        if args.video:
            # Import lazy: lingkungan tanpa requirements-ml.txt tetap bisa menjalankan server inti.
            from .video_input.service import start_video_thread
            start_video_thread(server, args.video, target_fps=args.video_fps, loop=args.video_loop)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
