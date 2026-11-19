"""Domain murni — zona bahaya ground-plane dalam meter.

Keputusan owner 8 Nov 2026: radius zona confirm default 4 meter; besar radius bisa
diatur pemanggil (arg CLI `--zone-radius`, env `ZONE_RADIUS_M`, atau parameter
`RiskEvaluator(zone_radius_m=...)` — jalur "input user berapa meter" yang
kelak tersambung ke antarmuka penugasan). Histeresis jarak: zona yang sudah
aktif dipertahankan sampai person keluar clear radius (default 6 m, konstanta
riset workspace; diatur lewat konstruktor `clear_radius_m`). Transisi status
dikontrol histeresis waktu di core/hazard.py (M4).

Titik tanah objek = bottom-center bounding box (perkiraan kaki person /
garis pondasi excavator) — penyaring tracking (equipmentInstanceId) akan
menyempurnakan titik ini.

Zona high-risk (keputusan owner 8-9 Nov 2026): radius high-risk = setengah
radius regulasi (DEFAULT_HIGH_RISK_RATIO = 0.5; regulasi 4 m -> high-risk 2 m).
Status tri-level per unit: clear | warning (<= regulasi, band histeresis sampai
clear) | danger (<= high-risk).

Modul domain murni: stdlib saja; kalibrasi di-duck-type (`to_meter`),
tidak diimpor — numpy tetap urusan core/calibration saja.
"""
import math

DEFAULT_ZONE_RADIUS_M = 4.0    # radius confirm (keputusan owner 8 Nov 2026)
DEFAULT_CLEAR_RADIUS_M = 6.0   # radius clear — histeresis jarak (riset workspace)
DEFAULT_HIGH_RISK_RATIO = 0.5  # high-risk = setengah radius regulasi (keputusan 8-9 Nov)
# Aset v4: offset titik tanah excavator dari tepi bawah bbox (persen tinggi).
EXCAVATOR_MARGIN_RATIO = 0.15

CLEAR = 'clear'
WARNING = 'warning'
DANGER = 'danger'


def high_risk_radius(radius_m, ratio=DEFAULT_HIGH_RISK_RATIO):
    """Radius zona high-risk = radius regulasi * rasio (default 0.5 — setengah)."""
    return radius_m * ratio


def persons_within_high_risk(persons, excavators, calibration, high_radius_m):
    """Hitung person yang berada dalam high_radius_m meter dari titik tanah
    excavator mana pun.

    Semantik identik `persons_within_radius` — hanya radius yang berbeda (zona
    nested high-risk). Titik tak terukur dilewati (lihat persons_within_radius).
    """
    return persons_within_radius(persons, excavators, calibration, high_radius_m)


def _nearest_person_distance(persons, excavators, calibration):
    """Jarak meter minimum person->excavator terukur (None bila tak ada).

    Pusat zona excavator = `excavator_foot` (bottom-center − 15% tinggi,
    aset v4); person diukur dari titik tanahnya.
    """
    if not persons or not excavators:
        return None
    centers = []
    for excavator in excavators:
        center = calibration.to_meter(*excavator_foot(excavator))
        if center is not None:
            centers.append(center)
    if not centers:
        return None
    nearest = None
    for person in persons:
        point = calibration.to_meter(*ground_point(person))
        if point is None:
            continue
        dist = min(math.dist(point, center) for center in centers)
        nearest = dist if nearest is None else min(nearest, dist)
    return nearest


def zone_status_for_unit(persons, excavators, calibration,
                         zone_radius_m=DEFAULT_ZONE_RADIUS_M,
                         clear_radius_m=DEFAULT_CLEAR_RADIUS_M,
                         high_risk_ratio=DEFAULT_HIGH_RISK_RATIO):
    """Status zona tri-level per unit (keputusan 13; api-draft §4.4).

    danger  — ada person <= high-risk (½ regulasi, default 2 m)
    warning — person terdekat <= clear_radius tapi > high-risk (dalam zona
              regulasi 4 m + band histeresis 4–6 m — zona masih aktif)
    clear   — person terdekat > clear_radius, atau tidak ada person terukur.
    """
    nearest = _nearest_person_distance(persons, excavators, calibration)
    if nearest is None:
        return CLEAR
    high_radius = high_risk_radius(zone_radius_m, high_risk_ratio)
    if nearest <= high_radius:
        return DANGER
    if nearest <= clear_radius_m:
        return WARNING
    return CLEAR


def ground_point(detection):
    """Titik tanah deteksi: bottom-center bounding box (x-tengah, y-bawah)."""
    x1, y1, x2, y2 = detection['box']
    return ((x1 + x2) / 2, y2)


def excavator_foot(detection, ratio=EXCAVATOR_MARGIN_RATIO):
    """Titik zona excavator: bottom-center bbox − 15% tinggi ke atas.

    Aset prototype v4 (vision/pipeline.py, EXCAVATOR_MARGIN_RATIO): titik
    berdiri/track mesin sedikit di atas tepi bawah bbox. Dipakai evaluator
    meter DAN render overlay — satu titik konsisten (keputusan owner 12 Nov,
    menggantikan eksperimen pusat-65% malam sebelumnya). Person tetap
    `ground_point` (kaki di tanah).
    """
    x1, y1, x2, y2 = detection['box']
    return ((x1 + x2) / 2, y2 - (y2 - y1) * ratio)


def persons_within_radius(persons, excavators, calibration, radius_m):
    """Hitung person yang berada dalam radius_m meter dari pusat zona
    excavator mana pun (`excavator_foot`, bottom-center − 15% tinggi).

    calibration: objek dengan `to_meter(x, y) -> (X, Y) | None`
    (GroundCalibrationV2). Titik yang None (proyeksi gagal — W≈0, dekat
    horizon) dilewati — person di luar bidang kalibrasi tidak dinilai; jika
    semua titik excavator None, zona dihitung kosong (dokumen: "tak terukur"
    vs "benar-benar kosong" dibedakan oleh histeresis M4).
    """
    if not persons or not excavators:
        return 0
    centers = []
    for excavator in excavators:
        center = calibration.to_meter(*excavator_foot(excavator))
        if center is not None:
            centers.append(center)
    if not centers:
        return 0
    count = 0
    for person in persons:
        point = calibration.to_meter(*ground_point(person))
        if point is None:
            continue
        if any(math.dist(point, center) <= radius_m for center in centers):
            count += 1
    return count
