"""Domain murni server — kontrak identitas kelas dan bentuk deteksi.

Inti kontrak (mirror models/README.md, "KTP" model v8):
    0 = excavator, 1 = excavator_arm, 2 = person.
Evaluasi risiko dan CLI menghitung via classId yang stabil; string label hanya
tampilan yang bisa berbeda antar bobot model.

`trackId` (opsional, M4): identifikasi track multi-object dari adapter
tracking nanti; kunci equipmentInstanceId di evaluator (state per unit).
TANPA trackId, semua box excavator dianggap satu zona anonim per sumber
(placeholder — identitas stabil menyusul modul tracking).

Modul ini TIDAK boleh mengimpor modul Sources lain (aturan dependency
clean-lite, CONTEXT.md 7 Nov 2026). Stdlib + typing_extensions saja.
"""
from typing_extensions import NotRequired, TypedDict


class Detection(TypedDict):
    """Bentuk deteksi netral lintas pipeline (camelCase mengikuti Contracts)."""

    classId: int
    label: str
    confidence: float
    box: tuple[int, int, int, int]  # piksel frame asli: (x1, y1, x2, y2)
    trackId: NotRequired[str]      # identitas track — kunci zona per-unit (M4)
    operator: NotRequired[bool]     # person hasil tanda mark_operators: operator di dalam excavator


CLASS_EXCAVATOR = 0
CLASS_EXCAVATOR_ARM = 1
CLASS_PERSON = 2
CLASS_NAMES = {
    CLASS_EXCAVATOR: 'excavator',
    CLASS_EXCAVATOR_ARM: 'excavator_arm',
    CLASS_PERSON: 'person',
}

# Person yang >=80% areanya berada di dalam bbox excavator dianggap operator
# (keputusan owner 11 Nov 2026): bukan bahaya, tak dihitung zona, tak dijadikan
# patokan ruler; box-nya TETAP digambar di frame.
OPERATOR_CONTAINMENT = 0.8


def mark_operators(detections, containment=OPERATOR_CONTAINMENT):
    """Tandai person = operator bila bbox-nya nyaris seluruhnya di dalam bbox
    excavator mana pun (containment >= 0.8 luas bbox person).

    Person pejalan tak pernah masuk: overlap sebagian (berdiri di samping/
    depan mesin) menghasilkan containment < 1; operator di kabin terlihat
    sebagai person bbox utuh yang "mengambang" di badan mesin → containment
    ~1. Pure domain: tanpa import modul lain; mark in-place + return list.
    """
    excavator_boxes = [d['box'] for d in detections if d.get('classId') == CLASS_EXCAVATOR]
    for detection in detections:
        if detection.get('classId') != CLASS_PERSON:
            continue
        detection['operator'] = any(
            _containment(detection['box'], box) >= containment
            for box in excavator_boxes
        )
    return detections


def _containment(person_box, excavator_box):
    """Porsi luas bbox person yang berada di dalam bbox excavator (0..1)."""
    px1, py1, px2, py2 = person_box
    ex1, ey1, ex2, ey2 = excavator_box
    person_area = max(0, px2 - px1) * max(0, py2 - py1)
    if person_area <= 0:
        return 0.0
    ix = max(0, min(px2, ex2) - max(px1, ex1))
    iy = max(0, min(py2, ey2) - max(py1, ey1))
    return (ix * iy) / person_area
