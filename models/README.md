# Model deteksi

Bobot model **ikut repository** di folder ini.

| File | Isi |
| --- | --- |
| `best_v8_3class.pt` | 5.516.421 bytes (≈5,3 MiB) — YOLO26n hasil training v8 |
| `README.md` | Dokumen ini |

Ukuran kecil khas yolo26n; sesuai kesepakatan tim aman di-commit (aturan repo hanya
melarang kredensial, data database, backup, dan rekaman video — bukan bobot model).

## Versi aktif: v8 (3-kelas)

- Training 5–6 Nov 2026 (Kaggle, yolo26n, 30 epoch, imgsz 1280, dataset v6 25k+ img).
- Test mAP50 **0,843** — excavator 0,913 · excavator_arm 0,837 · person 0,778.
- Kalibrasi inference server: conf 0,40, imgsz 1280, `DETECTION_CLASSES=0,2`.

## ⚠️ Urutan kelas v8 (BEDA dari Core ML prototipe lama)

| classId | v8 (model ini) | Core ML prototipe lama |
| --- | --- | --- |
| 0 | **excavator** | person |
| 1 | excavator_arm | excavator |
| 2 | person | — |

Jangan pernah menghapus peringatan ini — salah pasang ID kelas = alert salah rute.

## Pemakaian

Server memuat file ini secara otomatis (`Sources/detection/model.py` → `DEFAULT_MODEL_PATH`);
tidak perlu konfigurasi apa pun. `DETECTION_MODEL_PATH` di `.env` hanya untuk menimpa
dengan model lain (mis. hasil eksperimen) — lihat `.env.example`.

Ganti model = ganti file + update tabel metrik di atas + verifikasi:

```sh
.venv/bin/python - <<'PY'
import torch
ck = torch.load("models/best_v8_3class.pt", map_location="cpu", weights_only=False)
print(ck["model"].names)  # harus {0: 'excavator', 1: 'excavator_arm', 2: 'person'}
PY
```
