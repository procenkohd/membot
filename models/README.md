# Локальный классификатор фотографий

MobileNetV2 ImageNet-1K из [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/image_classification_mobilenet).

- Исходный файл: `image_classification_mobilenetv2_2022apr.onnx`.
- Закреплённая ревизия: `47534e27c9851bb1128ccc0102f1145e27f23f98`.
- SHA-256: `c0c3f76d93fa3fd6580652a45618618a220fced18babf65774ed169de0432ad5`.
- `labels.json` извлечён из `LABELS_IMAGENET_1K` в `mobilenet.py` этой ревизии.
- Лицензия модели и списка классов — Apache 2.0, полный текст в `LICENSE`.

Модель включена в репозиторий (14 МБ), поэтому работает после обычного
обновления с GitHub без ключей API, дополнительных установщиков и скачивания
весов при первом запросе. Изображения обрабатываются на хосте бота.
Распознаются только грубые предметные темы; эмоции и смысл сцены не определяются.
