"""Facecam detection. Streamers keep their webcam in a fixed corner, so we sample frames across the video,
detect faces (OpenCV Haar cascade, CPU-only), and look for a face that stays in the same place.
Returns a normalised rect {"x","y","w","h"} around the webcam area, or None. Always overridable in the UI.
"""
import os
import tempfile

import numpy as np

from . import media


def cluster_faces(dets: list[list[tuple[float, float, float, float]]], min_share: float = 0.4):
    """dets: per sampled frame, a list of normalised (cx, cy, w, h) faces. Returns the most stable face."""
    flat = [d for frame in dets for d in frame]
    if not flat or not dets:
        return None
    best, best_n = None, 0
    for cx, cy, w, h in flat:
        frames_hit, members = 0, []
        for frame in dets:
            near = [f for f in frame if abs(f[0] - cx) < 0.06 and abs(f[1] - cy) < 0.08 and 0.5 < f[2] / w < 2.0]
            if near:
                frames_hit += 1
                members.append(near[0])
        if frames_hit > best_n:
            best_n, best = frames_hit, members
    if best is None or best_n / len(dets) < min_share:
        return None
    m = np.median(np.array(best), axis=0)
    return {"cx": float(m[0]), "cy": float(m[1]), "w": float(m[2]), "h": float(m[3]), "share": best_n / len(dets)}


def face_to_cam_rect(face: dict, aspect: float) -> dict:
    """Expand a face box to a plausible webcam frame (head and shoulders), clamped to the picture."""
    w = min(1.0, face["w"] * 3.0)
    h = min(1.0, face["h"] * 2.6)
    x = face["cx"] - w / 2
    y = face["cy"] - h * 0.42
    x = float(np.clip(x, 0, 1 - w))
    y = float(np.clip(y, 0, 1 - h))
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def detect(src: str, duration: float, samples: int = 14) -> dict | None:
    try:
        import cv2
    except ImportError:
        return None
    name = "haarcascade_frontalface_default.xml"
    bundled = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", name)   # shipped with the app
    path = bundled if os.path.exists(bundled) else os.path.join(getattr(cv2.data, "haarcascades", ""), name)
    if not os.path.exists(path):
        return None
    casc = cv2.CascadeClassifier(path)
    if casc.empty():
        return None
    dets, aspect = [], 16 / 9
    with tempfile.TemporaryDirectory() as td:
        for i in range(samples):
            t = duration * (i + 0.5) / samples
            fp = os.path.join(td, f"{i}.jpg")
            if not media.grab_frame(src, t, fp, width=960):
                continue
            img = cv2.imread(fp)
            if img is None:
                continue
            H, W = img.shape[:2]
            aspect = W / H
            gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
            faces = casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(int(W * 0.03), int(W * 0.03)))
            # a webcam face is small relative to the frame; a face filling the frame is a cutscene close-up
            dets.append([((x + w / 2) / W, (y + h / 2) / H, w / W, h / H) for x, y, w, h in faces if w / W < 0.3])
    face = cluster_faces(dets)
    return face_to_cam_rect(face, aspect) if face else None
