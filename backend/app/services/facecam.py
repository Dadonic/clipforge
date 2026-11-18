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
    """Expand a face box to a plausible webcam frame (head and shoulders). A webcam in a corner is cut
    off by the edge of the picture, so the frame is clipped to the picture rather than pushed back into it."""
    w, h = face["w"] * 3.0, face["h"] * 2.6
    x0, y0 = face["cx"] - w / 2, face["cy"] - h * 0.42
    x1, y1 = min(1.0, x0 + w), min(1.0, y0 + h)
    x0, y0 = max(0.0, x0), max(0.0, y0)
    return {"x": round(x0, 4), "y": round(y0, 4), "w": round(x1 - x0, 4), "h": round(y1 - y0, 4)}


ASSETS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")   # detectors shipped with the app


def _yunet(cv2):
    """Neural face detector (YuNet). Finds tilted and partly turned faces that the Haar cascade misses,
    which is the normal state of a streamer looking at the game."""
    path = os.path.join(ASSETS, "face_detection_yunet_2023mar.onnx")
    if not os.path.exists(path) or not hasattr(cv2, "FaceDetectorYN"):
        return None
    try:
        det = cv2.FaceDetectorYN.create(path, "", (960, 540), 0.6, 0.3, 50)
    except Exception:
        return None

    def find(img):
        H, W = img.shape[:2]
        det.setInputSize((W, H))
        _, faces = det.detect(img)
        return [(float(f[0]), float(f[1]), float(f[2]), float(f[3])) for f in (faces if faces is not None else [])]
    return find


def _haar(cv2):
    name = "haarcascade_frontalface_default.xml"
    bundled = os.path.join(ASSETS, name)
    path = bundled if os.path.exists(bundled) else os.path.join(getattr(cv2.data, "haarcascades", ""), name)
    if not os.path.exists(path):
        return None
    casc = cv2.CascadeClassifier(path)
    if casc.empty():
        return None

    def find(img):
        W = img.shape[1]
        gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        return [tuple(map(float, f)) for f in
                casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(int(W * 0.03), int(W * 0.03)))]
    return find


def detect(src: str, duration: float, samples: int = 24) -> dict | None:
    try:
        import cv2
    except ImportError:
        return None
    for make in (_yunet, _haar):
        find = make(cv2)
        if not find:
            continue
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
                try:
                    faces = find(img)
                except Exception:
                    faces = []
                # a webcam face is small relative to the frame; a face filling the frame is a cutscene close-up
                dets.append([((x + w / 2) / W, (y + h / 2) / H, w / W, h / H) for x, y, w, h in faces
                             if 0.03 < w / W < 0.3])
        face = cluster_faces(dets)
        if face:
            return face_to_cam_rect(face, aspect)
    return None
