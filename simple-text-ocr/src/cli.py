import argparse
from pathlib import Path
from .ocr import ocr_with_easyocr
from .embeddings import SimpleEmbedder


def main():
    p = argparse.ArgumentParser(description="Card OCR and match demo")
    p.add_argument('--image', '-i', required=True, help='Path to image')
    p.add_argument('--use-tesseract', action='store_true')
    args = p.parse_args()

    img = Path(args.image)
    if not img.exists():
        print('Image not found:', img)
        return

    if args.use_tesseract:
        text = __import__('src.ocr', fromlist=['']).ocr_with_tesseract(str(img))
    else:
        text = ocr_with_easyocr(str(img))

    print('--- OCR output ---')
    print(text)

    print('\n--- Embedding (first 10 dims) ---')
    emb = SimpleEmbedder().embed(str(img))
    print(emb[:10])


if __name__ == '__main__':
    main()
