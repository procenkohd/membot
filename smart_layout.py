"""Лёгкий локальный анализ фотографии для размещения мемных подписей.

OpenCV используется только для лиц. Карта визуальной загруженности строится
через Pillow, поэтому при проблеме с OpenCV компоновщик продолжит работать и
будет избегать хотя бы участков с большим количеством мелких деталей.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from PIL import Image, ImageFilter, ImageOps, ImageStat

try:  # pragma: no cover - ветка без cv2 проверяется отдельным fallback-тестом
    import cv2
    import numpy as np
except Exception:  # безопасный fallback при отсутствии cv2 или системных библиотек
    cv2 = None
    np = None


Rect = tuple[int, int, int, int]


def _intersection_area(a: Rect, b: Rect) -> int:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    return max(0, right - left) * max(0, bottom - top)


def _expanded(rect: Rect, image_size: tuple[int, int], padding: float = 0.22) -> Rect:
    x, y, w, h = rect
    px, py = int(w * padding), int(h * padding)
    image_w, image_h = image_size
    left, top = max(0, x - px), max(0, y - py)
    right, bottom = min(image_w, x + w + px), min(image_h, y + h + py)
    return left, top, right - left, bottom - top


def _likely_subject_below_face(face: Rect, image_size: tuple[int, int]) -> Rect:
    """Примерная зона человека под лицом.

    Это не жёсткий запрет: каскад знает только про лицо, а мягкий штраф
    помогает выбрать пустую стену вместо футболки, если стена вообще есть.
    """
    x, y, w, h = face
    image_w, image_h = image_size
    left = max(0, x - int(w * 0.75))
    top = max(0, y + int(h * 0.55))
    right = min(image_w, x + w + int(w * 0.75))
    bottom = min(image_h, y + int(h * 6.0))
    return left, top, max(0, right - left), max(0, bottom - top)


@dataclass
class VisualAnalysis:
    image_size: tuple[int, int]
    detail_map: Image.Image
    faces: list[Rect]

    def detail_score(self, rect: Rect) -> float:
        image_w, image_h = self.image_size
        map_w, map_h = self.detail_map.size
        x, y, w, h = rect
        box = (
            max(0, int(x * map_w / image_w)),
            max(0, int(y * map_h / image_h)),
            min(map_w, int((x + w) * map_w / image_w)),
            min(map_h, int((y + h) * map_h / image_h)),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            return 1.0
        return ImageStat.Stat(self.detail_map.crop(box)).mean[0] / 255.0

    def face_overlap(self, rect: Rect) -> float:
        area = max(1, rect[2] * rect[3])
        overlap = sum(
            _intersection_area(rect, _expanded(face, self.image_size))
            for face in self.faces
        )
        return min(1.0, overlap / area)

    def subject_overlap(self, rect: Rect) -> float:
        area = max(1, rect[2] * rect[3])
        overlap = sum(
            _intersection_area(rect, _likely_subject_below_face(face, self.image_size))
            for face in self.faces
        )
        return min(1.0, overlap / area)

    def score(self, rect: Rect, occupied: Iterable[Rect] = ()) -> float:
        """Меньше — лучше. Лица существенно важнее фоновых деталей."""
        face_overlap = self.face_overlap(rect)
        score = self.detail_score(rect) * 3.2
        if face_overlap:
            score += 12.0 + face_overlap * 35.0
        # Тело под найденным лицом — мягкий штраф. В отличие от лица текст
        # всё ещё может попасть сюда, если свободного фона на фото нет.
        score += self.subject_overlap(rect) * 6.0
        area = max(1, rect[2] * rect[3])
        score += sum(_intersection_area(rect, other) / area * 20.0 for other in occupied)
        return score


@lru_cache(maxsize=1)
def _face_cascades():
    if cv2 is None:
        return ()
    return (
        cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml"),
        cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml"),
    )


def _detect_faces(image: Image.Image) -> list[Rect]:
    if cv2 is None or np is None:
        return []

    original_w, original_h = image.size
    ratio = min(1.0, 720 / max(original_w, original_h))
    scan_w, scan_h = max(1, int(original_w * ratio)), max(1, int(original_h * ratio))
    gray_pil = ImageOps.grayscale(image).resize((scan_w, scan_h), Image.Resampling.LANCZOS)
    gray = np.asarray(gray_pil)
    min_face = max(28, min(scan_w, scan_h) // 14)

    cascades = _face_cascades()
    found: list[tuple[int, int, int, int]] = []
    for cascade in cascades:
        if cascade.empty():
            continue
        found.extend(
            tuple(map(int, face))
            for face in cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_face, min_face)
            )
        )

    # Профильный каскад смотрит только в одну сторону — зеркальный проход
    # находит лица, повёрнутые в другую.
    profile = cascades[-1]
    if not profile.empty():
        for x, y, w, h in profile.detectMultiScale(
                cv2.flip(gray, 1), scaleFactor=1.1, minNeighbors=5,
                minSize=(min_face, min_face)):
            found.append((scan_w - int(x) - int(w), int(y), int(w), int(h)))

    scaled: list[Rect] = []
    for x, y, w, h in found:
        rect = (
            int(x / ratio), int(y / ratio),
            int(w / ratio), int(h / ratio),
        )
        # Каскады иногда находят одно лицо несколько раз.
        if not any(_intersection_area(rect, old) > min(rect[2] * rect[3], old[2] * old[3]) * 0.45
                   for old in scaled):
            scaled.append(rect)
    return scaled


def analyze_image(image: Image.Image) -> VisualAnalysis:
    image = image.convert("RGB")
    image_w, image_h = image.size
    map_w = min(360, image_w)
    map_h = max(1, int(image_h * map_w / image_w))
    gray = ImageOps.grayscale(image).resize((map_w, map_h), Image.Resampling.LANCZOS)
    # FIND_EDGES подчёркивает места, где текст сильнее всего мешает фотографии.
    detail = gray.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(2.0))
    detail = ImageOps.autocontrast(detail, cutoff=1)
    return VisualAnalysis(image.size, detail, _detect_faces(image))
