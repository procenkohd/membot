"""Local face detection and conservative detail protection, not object recognition.

Uses cascades shipped with the installed OpenCV wheel. No network or photos
written to disk. A small bounded cache serves repeated captions on the same photo.
Cascade API: https://docs.opencv.org/4.10.0/db/d28/tutorial_cascade_classifier.html
"""
from dataclasses import dataclass
from functools import lru_cache
import logging
import threading

from PIL import Image

_lock = threading.Lock()


@dataclass(frozen=True)
class Protection:
    width: int
    height: int
    faces: tuple
    details: bytes
    uncertain: bool = False

    def overlaps(self, box, image_size) -> bool:
        if self.uncertain:
            return True
        import numpy as np
        w, h = image_size
        x0, y0, x1, y1 = box
        x0, x1 = max(0, int(x0 * self.width / w)), min(self.width, int(x1 * self.width / w) + 1)
        y0, y1 = max(0, int(y0 * self.height / h)), min(self.height, int(y1 * self.height / h) + 1)
        if x1 <= x0 or y1 <= y0:
            return False
        for fx0, fy0, fx1, fy1 in self.faces:
            if x0 < fx1 and x1 > fx0 and y0 < fy1 and y1 > fy0:
                return True
        mask = np.frombuffer(self.details, dtype=np.uint8).reshape(self.height, self.width)
        # Busy background / high-contrast foreground is treated conservatively.
        return float(mask[y0:y1, x0:x1].mean()) > 0.10


@lru_cache(maxsize=1)
def _cascades():
    import cv2
    names = ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml",
             "haarcascade_frontalcatface_extended.xml")
    detectors = tuple(cv2.CascadeClassifier(cv2.data.haarcascades + name) for name in names)
    if any(detector.empty() for detector in detectors):
        raise RuntimeError("Face cascade missing from OpenCV installation")
    return detectors


@lru_cache(maxsize=12)
def _analyze(width: int, height: int, rgb: bytes) -> Protection:
    import cv2
    import numpy as np
    image = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
    gray = cv2.equalizeHist(cv2.cvtColor(image, cv2.COLOR_RGB2GRAY))
    faces = []
    with _lock:
        for i, detector in enumerate(_cascades()):
            for flipped in (False, True) if i == 1 else (False,):
                work = cv2.flip(gray, 1) if flipped else gray
                for x, y, w, h in detector.detectMultiScale(work, scaleFactor=1.12, minNeighbors=4, minSize=(24, 24)):
                    if flipped:
                        x = width - x - w
                    pad_x, pad_y = int(w * .18), int(h * .22)
                    faces.append((max(0, x-pad_x), max(0, y-pad_y),
                                  min(width, x+w+pad_x), min(height, y+h+pad_y)))
    # Detect detail/contrast, not semantic objects. Solid backgrounds stay free;
    # textured backgrounds and salient colour patches often trigger safe bands.
    smooth = cv2.GaussianBlur(image, (5, 5), 0).astype(np.float32)
    background = np.median(smooth.reshape(-1, 3), axis=0)
    contrast = np.linalg.norm(smooth - background, axis=2) > 65
    edges = cv2.Canny(cv2.cvtColor(image, cv2.COLOR_RGB2GRAY), 60, 140)
    edges = cv2.dilate(edges, np.ones((5, 5), dtype=np.uint8)) > 0
    mask = (contrast | edges).astype(np.uint8)
    return Protection(width, height, tuple(faces), mask.tobytes())


def analyze(image: Image.Image) -> Protection:
    try:
        small = image.convert("RGB")
        small.thumbnail((384, 384), Image.Resampling.BILINEAR)
        return _analyze(small.width, small.height, small.tobytes())
    except Exception:
        logging.getLogger(__name__).exception("Caption safety unavailable; using outside bands")
        # Failure must not put text over an unexamined face or break the bot.
        return Protection(1, 1, (), b"\0", uncertain=True)
