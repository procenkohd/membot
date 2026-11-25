"""Offline visual check: python tools/preview_meme_layout.py photo.jpg output.jpg."""
from io import BytesIO
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from PIL import Image
import memegen


def main():
    source, destination = map(Path, sys.argv[1:3])
    image_bytes = source.read_bytes()
    result = memegen.make_classic_meme(
        image_bytes, "ДОКАЗАТЕЛЬСТВО ТОГО,", "ЧТО НОВЫЕ ТЕХНОЛОГИИ ОПИРАЮТСЯ НА СТАРЬЁ")
    Image.open(BytesIO(result.getvalue())).save(destination)
    print({"layout": result.safety, "preview": str(destination.resolve())})


if __name__ == "__main__":
    main()
