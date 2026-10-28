"""Find the streamer's facecam in a clip so the Short's cam box follows it.

The backlog scenes put the background-removed facecam in different places (bigger on
2026-09-10/11, further right from 09-15, tiny on 09-28_2), so one fixed crop doesn't
fit. We sample a few frames, detect faces in the bottom-right part of the frame
(where the cam always sits), take a robust median, and build a crop with the
layout's cam aspect around it. No face found: the layout's measured crop is used.
"""

from __future__ import annotations

import json
import statistics
import subprocess
from pathlib import Path
from typing import Optional

SRC_W, SRC_H = 1920, 1080        # reference pixels (media.REF_W/REF_H): crops come out in these at any recording size
SCALE = 0.5                      # detect on 960x540 frames (the recording is scaled to 960 wide, so 2K lands here too)
REGION = (0.45, 0.35, 1.0, 1.0)  # default search area for the face centre: x0, y0, x1, y1 as frame fractions
CROP_PER_FACE = 3.4              # crop width in face widths
MIN_W, MAX_W = 340, 760


def _frame(rec: Path, t: float):
    import cv2
    import numpy as np

    p = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.3f}", "-i", str(rec), "-frames:v", "1",
         "-vf", f"scale={int(SRC_W * SCALE)}:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True,
    )
    if p.returncode or not p.stdout:
        return None
    return cv2.imdecode(np.frombuffer(p.stdout, np.uint8), cv2.IMREAD_GRAYSCALE)


def detect_faces(rec: Path, start: float, dur: float, samples: int = 9,
                 region: tuple[float, float, float, float] = REGION) -> list[tuple[float, float, float]]:
    """(cx, cy, size) in reference pixels for the largest in-region face of each sampled frame."""
    found = []
    for i in range(samples):
        img = _frame(rec, start + dur * (i + 0.5) / samples)
        if img is None:
            continue
        best = largest_face(img, region, SCALE)
        if best:
            found.append(best)
    return found


def largest_face(gray, region: tuple[float, float, float, float] = REGION,
                 scale: float = 1.0, min_neighbors: int = 6) -> Optional[tuple[float, float, float]]:
    """(cx, cy, size) in source pixels of the largest face centred in ``region`` of a
    grayscale frame scaled by ``scale``; None if there isn't one."""
    import cv2

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    h, w = gray.shape
    best = None
    for (x, y, fw, fh) in cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=min_neighbors, minSize=(36, 36)):
        cx, cy = x + fw / 2, y + fh / 2
        if not (region[0] * w <= cx <= region[2] * w and region[1] * h <= cy <= region[3] * h):
            continue
        if best is None or fw > best[2]:
            best = (cx / scale, cy / scale, fw / scale)
    return best


def cam_crop(faces: list[tuple[float, float, float]], aspect: float) -> Optional[tuple[int, int, int, int]]:
    """Crop [x, y, w, h] around the median face, ``aspect`` = h / w. None if too few detections."""
    if len(faces) < 2:
        return None
    mx, my = statistics.median(f[0] for f in faces), statistics.median(f[1] for f in faces)
    ms = statistics.median(f[2] for f in faces)
    keep = [f for f in faces if abs(f[0] - mx) < 1.5 * ms and abs(f[1] - my) < 1.5 * ms]
    if len(keep) < 2:
        return None
    cx, cy = statistics.median(f[0] for f in keep), statistics.median(f[1] for f in keep)
    size = statistics.median(f[2] for f in keep)
    w = int(min(MAX_W, max(MIN_W, CROP_PER_FACE * size)))
    h = int(round(w * aspect))
    x = int(round(cx - w / 2))
    y = int(round(cy - 0.42 * h))        # face a little above the middle, shoulders below
    x = max(0, min(SRC_W - w, x))
    y = max(0, min(SRC_H - h, y))
    return x - x % 2, y - y % 2, w - w % 2, h - h % 2


def auto_cam(rec: Path, start: float, dur: float, aspect: float, cache: Path,
             region: tuple[float, float, float, float] = REGION) -> Optional[tuple[int, int, int, int]]:
    """Detected cam crop for a clip, cached in ``cache`` (JSON) so re-renders match the preview."""
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        return tuple(data["crop"]) if data.get("crop") else None
    faces = detect_faces(rec, start, dur, region=region)
    crop = cam_crop(faces, aspect)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"crop": crop, "faces": [list(map(round, f)) for f in faces]}), encoding="utf-8")
    return crop
