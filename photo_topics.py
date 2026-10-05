"""Грубые темы фотографии: локальная MobileNetV2, без внешних API."""

from functools import lru_cache
from io import BytesIO
import json
import logging
from pathlib import Path
import re
import threading

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)
MODELS = Path(__file__).parent / "models"
_model_lock = threading.Lock()

# Индексы закреплённой ImageNet-1K модели (см. models/labels.json).
# Суммируем близкие породы, но не смешиваем всех животных в одну тему.
CLASS_TOPICS = {
    **{i: "cat" for i in range(281, 286)},
    **{i: "dog" for i in range(151, 269)},
    **{i: "bird" for i in (*range(7, 25), *range(80, 101), *range(127, 147))},
    **{i: "fish" for i in (*range(0, 7), *range(389, 398))},
    **{i: "frog" for i in range(30, 33)},
    **{i: "monkey" for i in range(365, 383)},
    **{i: "food" for i in (*range(924, 958), *range(959, 966))},
}

LABEL_TOPICS = {
    "car": ("beach wagon", "cab", "convertible", "jeep", "limousine", "minivan",
            "Model T", "racer", "sports car", "pickup", "car wheel", "car mirror"),
    "transport": ("airliner", "airship", "bullet train", "electric locomotive",
                  "steam locomotive", "minibus", "school bus", "streetcar", "trolleybus"),
    "computer": ("desktop computer", "laptop", "notebook", "computer keyboard",
                 "monitor", "mouse", "hard disc", "modem", "printer", "web site"),
    "phone": ("cellular telephone", "dial telephone", "pay-phone", "iPod"),
    "coffee": ("coffee mug", "coffeepot", "espresso", "espresso maker"),
    "alcohol": ("beer bottle", "beer glass", "wine bottle", "red wine", "whiskey jug"),
    "sleep": ("four-poster", "pillow", "quilt", "studio couch", "sleeping bag", "pajama"),
    "food": ("restaurant", "bakery", "grocery store", "confectionery", "dining table"),
    "nature": ("alp", "cliff", "geyser", "lakeside", "promontory", "sandbar",
               "seashore", "valley", "volcano"),
    "sport": ("barbell", "dumbbell", "basketball", "soccer ball", "tennis ball",
              "volleyball", "punching bag", "horizontal bar", "parallel bars"),
}

# Отдельно от «жёстче / абсурднее»: это предмет подписи, не её стиль.
# Границы слов не дают принять «который» за кота или «поездку» за еду.
TOPIC_PATTERNS = {
    "cat": r"\b(?:кот(?:а|у|ом|е|ы|ов|ик\w*|ен\w*)?|кош(?:к\w*|ач\w*))\b",
    "dog": r"\b(?:собак\w*|собач\w*|пес|пса|псу|псом|псе|псов|пси\w*|щен\w*)\b",
    "bird": r"\b(?:гусь|гуся|гусю|гусем|гуси\w*|гусе\w*|голуб\w*|птиц\w*|птич\w*|утк\w*|петух\w*|куриц\w*|попуга\w*)\b",
    "fish": r"\b(?:рыб\w*|акул\w*|карась|карася|карп\w*|селед\w*)\b",
    "frog": r"\b(?:жаб\w*|лягуш\w*)\b",
    "monkey": r"\b(?:обезьян\w*|горилл\w*|шимпанзе|макак\w*)\b",
    "car": r"\b(?:машин\w*|авто\w*|водител\w*|руль|руля|рулем|парков\w*|бензин\w*|гараж\w*|пробк\w*)\b",
    "transport": r"\b(?:поезд\w*|самолет\w*|автобус\w*|трамва\w*|метро|вокзал\w*|аэропорт\w*|пассажир\w*|билет\w*)\b",
    "computer": r"\b(?:комп\w*|ноут\w*|программист\w*|код|кода|кодом|баг\w*|сервер\w*|интернет\w*|браузер\w*|эксел\w*|винд\w*)\b",
    "phone": r"\b(?:телефон\w*|звон\w*|позвон\w*|уведомлен\w*|переписк\w*|сообщени\w*|смс|чат\w*)\b",
    "coffee": r"\b(?:кофе\w*|капучино|латте|эспрессо|кружк\w*)\b",
    "alcohol": r"\b(?:пив\w*|вино|вина|вином|виниш\w*|алкогол\w*|бух\w*|пьян\w*|похмел\w*|водк\w*|выпив\w*)\b",
    "sleep": r"\b(?:сон|сна|сне|сном|сны|спать|сплю|спит|спим|спят|высп\w*|недосып\w*|подуш\w*|одеял\w*|кроват\w*|диван\w*|будильник\w*)\b",
    "food": r"\b(?:еда|еды|еду|едой|еде|поес\w*|пожра\w*|жра\w*|обед\w*|ужин\w*|завтрак\w*|пельмен\w*|пицц\w*|суп|супа|супом|супе|борщ\w*|шаурм\w*|бутерброд\w*|котлет\w*|макарон\w*|картош\w*|холодильник\w*|колбас\w*|куша\w*)\b",
    "nature": r"\b(?:природ\w*|лес|леса|лесу|лесом|горы|горах|горе|море|моря|морем|пляж\w*|отпуск\w*|дерев\w*|озер\w*|поход\w*)\b",
    "sport": r"\b(?:спорт\w*|трениров\w*|тренер\w*|фитнес\w*|качал\w*|гантел\w*|футбол\w*|баскетбол\w*|мышц\w*|пресс|бегаю|бегу)\b",
}
_patterns = {topic: re.compile(pattern) for topic, pattern in TOPIC_PATTERNS.items()}


@lru_cache(maxsize=12000)
def phrase_topics(phrase: str) -> frozenset[str]:
    text = phrase.lower().replace("ё", "е")
    return frozenset(topic for topic, pattern in _patterns.items() if pattern.search(text))


@lru_cache(maxsize=1)
def _class_topics() -> dict[int, str]:
    labels = json.loads((MODELS / "labels.json").read_text(encoding="utf-8"))
    result = dict(CLASS_TOPICS)
    for topic, names in LABEL_TOPICS.items():
        for name in names:
            result[labels.index(name)] = topic
    return result


def topics_from_scores(scores) -> list[str]:
    """Принимает logits модели; отказывается от неуверенных догадок."""
    import numpy as np
    scores = np.asarray(scores, dtype=np.float64).reshape(-1)
    if scores.size != 1000 or not np.isfinite(scores).all():
        return []
    probs = np.exp(scores - scores.max())
    probs /= probs.sum()
    mapping = _class_topics()
    # Уверенность темы складывается только из пяти лучших классов.
    ranked = probs.argsort()[::-1][:5]
    topic = mapping.get(int(ranked[0]))
    if topic is None or probs[ranked[0]] < 0.12:
        return []
    confidence = sum(float(probs[i]) for i in ranked if mapping.get(int(i)) == topic)
    return [topic] if confidence >= 0.55 else []


@lru_cache(maxsize=1)
def _network():
    import cv2
    return cv2.dnn.readNetFromONNX(str(MODELS / "mobilenetv2.onnx"))


def analyze_topics(image_bytes: bytes) -> list[str]:
    """CPU, один проход на фото; модель ничего не скачивает во время запроса."""
    try:
        import numpy as np
        with Image.open(BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = image.resize((256, 256), Image.Resampling.BILINEAR).crop((16, 16, 240, 240))
        blob = np.asarray(image, dtype=np.float32) / 255.0
        blob = (blob - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
            [0.229, 0.224, 0.225], dtype=np.float32)
        blob = blob.transpose(2, 0, 1)[None].copy()
        # OpenCV Net хранит вход внутри себя и не допускает параллельный forward.
        with _model_lock:
            model = _network()
            model.setInput(blob)
            scores = model.forward().copy()
        return topics_from_scores(scores)
    except Exception:
        logger.exception("Image topic analysis unavailable; using random captions")
        return []
