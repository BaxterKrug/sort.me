"""
Robust OCR helper for card images.

Provides process_card_image(path_or_array, game='mtg') which returns a dict with
rotation metadata and per-region OCR text and confidences. Adapted from
the working simple-text-ocr implementation.
"""

from typing import Dict, Any, Optional, Callable, Tuple
import cv2
import numpy as np
from PIL import Image
import pytesseract
from pytesseract import Output
import difflib
import json
import os

# Simplified OCR: only perform a whole-image OCR and return a single 'full' region.


def load_image(path_or_array):
    if isinstance(path_or_array, str):
        img = cv2.imread(path_or_array, cv2.IMREAD_COLOR)
    else:
        img = path_or_array.copy()
    if img is None:
        raise FileNotFoundError("Could not load image")
    return img


def preprocess_for_ocr(img_gray: np.ndarray) -> np.ndarray:
    # Improved preprocessing pipeline to boost OCR quality:
    # - apply CLAHE for contrast
    # - bilateral filter to reduce noise while keeping edges
    # - upscale to help tesseract read small text
    # - median blur + adaptive threshold
    h, w = img_gray.shape[:2]
    # apply CLAHE (contrast limited adaptive histogram equalization)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    img_clahe = clahe.apply(img_gray)
    # bilateral filter preserves edges
    img_bilat = cv2.bilateralFilter(img_clahe, d=9, sigmaColor=75, sigmaSpace=75)
    # upscale more aggressively to help small text
    scale = 3
    img_up = cv2.resize(img_bilat, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
    # median blur to remove salt-and-pepper
    img_med = cv2.medianBlur(img_up, 3)
    # adaptive threshold with larger block size and empirical C
    img_thresh = cv2.adaptiveThreshold(img_med, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, 15, 9)
    # morphological opening to remove small noise
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    img_open = cv2.morphologyEx(img_thresh, cv2.MORPH_OPEN, kernel)
    return img_open


def ocr_image_full(img_bgr, lang='eng', psm=6) -> Tuple[str, float, dict]:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    prep = preprocess_for_ocr(gray)
    pil = Image.fromarray(prep)
    config = f'--psm {psm} --oem 3'
    data = pytesseract.image_to_data(pil, lang=lang, config=config, output_type=Output.DICT)

    # join all non-empty words as a single text blob
    words = [t.strip() for t in data.get('text', []) if t and t.strip()]
    text = " ".join(words).strip()

    confs = []
    for c in data.get('conf', []):
        try:
            ci = float(c)
            if ci >= 0:
                confs.append(ci)
        except Exception:
            pass
    avg_conf = float(sum(confs)) / len(confs) if confs else 0.0
    return text, avg_conf, data


def _keep_english_letters(text: str) -> str:
    """Return text containing only A-Z, a-z and spaces. Collapse whitespace."""
    import re
    if not text:
        return ""
    # Replace any character that is not an English letter with a space
    cleaned = re.sub(r"[^A-Za-z]+", " ", text)
    # Collapse multiple spaces and strip
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


_CORRECTION_WORDS = None


def _load_correction_words() -> list:
    """Load a list of candidate words from the cards metadata to use for lightweight OCR correction.

    Returns a sorted list of unique lower-case words (alphabetic only). Caches the result.
    """
    global _CORRECTION_WORDS
    if _CORRECTION_WORDS is not None:
        return _CORRECTION_WORDS
    path = os.path.join("data", "embeddings", "cards_metadata.json")
    words = set()
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf8') as fh:
                meta = json.load(fh)
            for card in meta:
                name = card.get('name') or card.get('title') or ''
                for tok in name.split():
                    tok2 = ''.join([c for c in tok if c.isalpha()])
                    if len(tok2) >= 2:
                        words.add(tok2.lower())
    except Exception:
        pass
    # fallback: small common words to avoid empty
    if not words:
        words = {w for w in [
            'the', 'and', 'of', 'to', 'a', 'in', 'for', 'you', 'your', 'when', 'target', 'creature', 'owner',
            'hand', 'draw', 'life', 'gain', 'card', 'battlefield', 'enters', 'exile'
        ]}
    _CORRECTION_WORDS = sorted(words)
    return _CORRECTION_WORDS


def _post_correct_text(text: str) -> str:
    """Simple word-level correction: for words not in dict, find a close match from card words.

    Only attempts correction for words length >= 4 to avoid over-correcting small words.
    """
    if not text:
        return text
    dict_words = _load_correction_words()
    if not dict_words:
        return text
    dict_set = set(dict_words)
    parts = text.split()
    out = []
    for w in parts:
        lw = w.lower()
        if lw in dict_set or len(w) < 4:
            out.append(w)
            continue
        # try to find a close match
        matches = difflib.get_close_matches(lw, dict_words, n=1, cutoff=0.78)
        if matches:
            # preserve capitalization if original looked capitalized
            match = matches[0]
            if w[0].isupper():
                out.append(match.capitalize())
            else:
                out.append(match)
        else:
            out.append(w)
    return ' '.join(out)


def process_card_image(path_or_array,
                       game: str = 'mtg',
                       lang: str = 'eng',
                       identifier_callback: Optional[Callable[[Dict[str, str]], Any]] = None
                       ) -> Dict[str, Any]:
    img = load_image(path_or_array)
    # use the full (inset slightly) image for OCR to avoid border artifacts
    h, w = img.shape[:2]
    inset = 0.02
    x0, y0 = int(w * inset), int(h * inset)
    x1, y1 = int(w * (1 - inset)), int(h * (1 - inset))
    crop = img[y0:y1, x0:x1]

    # Try two different psm modes and pick the one with higher average confidence
    text1, conf1, data1 = ocr_image_full(crop, lang=lang, psm=6)
    text2, conf2, data2 = ocr_image_full(crop, lang=lang, psm=11)
    if conf2 > conf1:
        text, conf, data = text2, conf2, data2
    else:
        text, conf, data = text1, conf1, data1
    # filter to only English letters for downstream processing
    text_filtered = _keep_english_letters(text)
    # apply lightweight post-correction based on card-word dictionary
    text_corrected = _post_correct_text(text_filtered)

    results = {
        'rotation_detected': 0,
        'rotation_confidence': 0.0,
        'regions': {
            'full': {
                'text': text_corrected,
                'confidence': conf,
                'box_shape': crop.shape[:2],
            }
        },
        'ocr': {
            'full': data
        }
    }

    if identifier_callback:
        try:
            id_result = identifier_callback({'full': text})
            results['identifier'] = id_result
        except Exception as e:
            results['identifier_error'] = str(e)

    return results


if __name__ == '__main__':
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: ocr.py <image>")
        sys.exit(1)
    out = process_card_image(sys.argv[1], game='mtg')
    print(json.dumps(out, indent=2, default=str))