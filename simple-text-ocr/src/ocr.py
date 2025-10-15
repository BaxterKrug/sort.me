import os
from typing import Optional
from PIL import Image
import pytesseract

try:
    import easyocr
    _HAS_EASYOCR = True
except Exception:
    _HAS_EASYOCR = False


def ocr_with_tesseract(image_path: str, lang: str = 'eng') -> str:
    """Extract text from image using Tesseract."""
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)
    img = Image.open(image_path)
    text = pytesseract.image_to_string(img, lang=lang)
    return text.strip()


def ocr_with_easyocr(image_path: str, lang_list: Optional[list] = None) -> str:
    """Extract text using EasyOCR if installed; falls back to Tesseract.

    Returns concatenated lines.
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)
    if not _HAS_EASYOCR:
        return ocr_with_tesseract(image_path)
    if lang_list is None:
        lang_list = ['en']
    reader = easyocr.Reader(lang_list, gpu=False)
    results = reader.readtext(image_path)
    # results is list of (bbox, text, conf)
    lines = [r[1] for r in results]
    return "\n".join(lines)
