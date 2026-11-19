# Detection model

Weights are bundled in this folder **and** on Hugging Face.

| File | Size | Source |
| --- | --- | --- |
| `best_v8_3class.pt` | 5.3 MiB (5,516,421 bytes) — YOLO26n v8 | `models/` + HF `Akhnaf03/HARNES-YOLOv8` |
| `Anything.pt` | 5.3 MiB — alias | same HF repo |

Small enough to stay in git (team rule only blocks credentials, DB dumps, backups, and raw recordings).

Hugging Face: **https://huggingface.co/Akhnaf03/HARNES-YOLOv8** — public, no token needed. The server auto-fetches from HF if the local file is missing (needs `huggingface_hub`, included in `requirements-ml.txt`). Override with `DETECTION_MODEL_PATH`, `HF_MODEL_REPO`, or `HF_MODEL_FILENAME`.

## Active version: v8 (3-class)

- Training 5-6 Nov 2026 (Kaggle, yolo26n, 30 epochs, imgsz 1280, dataset v6 25k+ images).
- Test mAP50 **0.843** — excavator 0.913, excavator_arm 0.837, person 0.778.
- Server inference defaults: conf 0.35, imgsz 1600, `DETECTION_CLASSES=0,2` (arm hidden for MVP; use `0,1,2` for arm-swing zones).

## ⚠️ Class order v8 (different from old Core ML prototype)

| classId | v8 (this model) | Old Core ML prototype |
| --- | --- | --- |
| 0 | **excavator** | person |
| 1 | excavator_arm | excavator |
| 2 | person | — |

Keep this warning. Wrong class mapping means wrong alert routing.

## Usage

Loaded automatically (`Sources/detection/model.py` → `DEFAULT_MODEL_PATH` → HF fallback). No config needed. `DETECTION_MODEL_PATH` in `.env` overrides with another checkpoint — see `.env.example`.

Verify:

```sh
.venv/bin/python - <<'PY'
import torch
ck = torch.load("models/best_v8_3class.pt", map_location="cpu", weights_only=False)
print(ck["model"].names)  # {0: 'excavator', 1: 'excavator_arm', 2: 'person'}
PY
# Fetch from HF directly:
# .venv/bin/python -c "from huggingface_hub import hf_hub_download; print(hf_hub_download('Akhnaf03/HARNES-YOLOv8','best_v8_3class.pt'))"
```
