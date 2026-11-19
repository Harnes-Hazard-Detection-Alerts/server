#!/usr/bin/env python3
"""Download sample CCTV recordings for reviewers (720p, ~64 MB total).

Reviewer workflow:
    .venv/bin/python Tools/fetch_recordings.py
    .venv/bin/python Tools/seed_demo.py --scenario dev --video-dir recordings

Videos are Pexels free-license samples, hosted as GitHub Release assets.
If download fails (offline), seed_demo still works — cameras are created
with or without video files.
"""
import sys
import urllib.request
import urllib.error
from pathlib import Path

REPO = "akhnafal-aban/HARNES-Server"
TAG = "recordings-720p"
FILES = [
    "pexels-kelly-2938859-4096x2160-24fps_720p.mp4",
    "pexels-tom-fisk-7518596-3840x2160-30fps_720p.mp4",
    "pexels-ye-qiao-5450903-3840x2160-30fps_720p.mp4",
]

def download(url: str, dest: Path):
    print(f"  ↓ {dest.name} ...", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length", 0))
            chunk = 1024 * 256
            done = 0
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if total:
                    pct = done * 100 // total
                    print(f"\r  ↓ {dest.name} {pct}%", end="", flush=True)
            print(f"\r  ✓ {dest.name} ({done // 1024 // 1024} MB)     ")
            return True
    except (urllib.error.URLError, OSError) as e:
        print(f"  ✗ {dest.name}: {e}")
        if dest.exists():
            dest.unlink()
        return False

def try_gh_download(out: Path):
    """For private repos, `gh release download` uses the authenticated token."""
    import subprocess
    import shutil
    if not shutil.which("gh"):
        return None
    print("  Trying `gh release download` (authenticated, works for private repos)...")
    result = subprocess.run(
        ["gh", "release", "download", TAG, "--repo", REPO, "--dir", str(out), "--clobber", "--pattern", "*720p*"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("  ✓ gh download succeeded")
        return True
    print(f"  gh download failed: {result.stderr.strip()[:200]}")
    return False

def main():
    out = Path(__file__).resolve().parents[1] / "recordings"
    out.mkdir(exist_ok=True)
    # Prefer `gh` for private repos; fallback to anonymous HTTPS for public repos.
    already = sum(1 for n in FILES if (out / n).exists() and (out / n).stat().st_size > 1024 * 1024)
    if already < len(FILES):
        gh_ok = try_gh_download(out)
        if gh_ok:
            ok = sum(1 for n in FILES if (out / n).exists() and (out / n).stat().st_size > 1024 * 1024)
            print(f"\nDone: {ok}/{len(FILES)} files ready → {out}")
            return
    base = f"https://github.com/{REPO}/releases/download/{TAG}"
    ok, skipped, failed = 0, 0, 0
    for name in FILES:
        dest = out / name
        if dest.exists() and dest.stat().st_size > 1024 * 1024:
            print(f"  = {name} already exists ({dest.stat().st_size // 1024 // 1024} MB) — skip")
            skipped += 1
            continue
        url = f"{base}/{name}"
        if download(url, dest):
            ok += 1
        else:
            failed += 1
    print(f"\nDone: {ok} downloaded, {skipped} existing, {failed} failed → {out}")
    if failed:
        print("Tip: for private repos use `gh auth login` then re-run, or download manually:")
        print(f"  gh release download {TAG} --repo {REPO} --pattern \"*720p*\" --dir recordings")
        print(f"  https://github.com/{REPO}/releases/tag/{TAG}")
        sys.exit(1)

if __name__ == "__main__":
    main()
