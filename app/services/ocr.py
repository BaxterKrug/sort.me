"""
Robust OCR helper for card images.

Features:
- Detects and fixes orientation (0/90/180/270) using an ensemble:
  pytesseract OSD if available, otherwise try all rotations and pick the one
  with the highest quick-OCR confidence.
- Performs targeted region crops per-game (MTG implemented) using stable
  proportional boxes (no brittle template matching).
- Preprocesses crops (upsample, denoise, adaptive-thresh) before OCR.
- Returns per-region text + confidence and an overall orientation/confidence.
- Calls an external card identifier via a callback (not implemented here).
"""

from typing import Tuple, Dict, Any, Optional, Callable
import cv2
import numpy as np
from PIL import Image
import pytesseract
from pytesseract import Output

# tuning params
UPSCALE = 2
DENOISE_H = 10

def load_image(path_or_array):
    if isinstance(path_or_array, str):
        img = cv2.imread(path_or_array, cv2.IMREAD_COLOR)
    else:
        img = path_or_array.copy()
    if img is None:
        raise FileNotFoundError("Could not load image")
    return img

def _pytess_osd_rotation(img_gray) -> Optional[int]:
    try:
        osd = pytesseract.image_to_osd(img_gray)
        # sample osd output: "Page number: 0\nOrientation in degrees: 90\nRotate: 90\nOrientation confidence: 9.06\n"
        for line in osd.splitlines():
            if "Orientation in degrees" in line or line.startswith("Rotate:"):
                parts = line.split(":")
                angle = int(parts[-1].strip())
                return angle % 360
    except Exception:
        return None

def _ocr_quick_conf(img_gray, lang='eng') -> float:
    # quick OCR on central band to estimate readability
    h, w = img_gray.shape[:2]
    cy1, cy2 = int(h*0.3), int(h*0.7)
    band = img_gray[cy1:cy2, int(w*0.1):int(w*0.9)]
    data = pytesseract.image_to_data(band, lang=lang, output_type=Output.DICT, config='--psm 6')
    confs = []
    for c in data.get('conf', []):
        try:
            ci = float(c)
            if ci >= 0:
                confs.append(ci)
        except Exception:
            pass
    if not confs:
        return 0.0
    return float(sum(confs)) / len(confs)

def detect_best_rotation(img_bgr, lang='eng') -> Tuple[int, float]:
    img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    # 1) try OSD
    osd_angle = _pytess_osd_rotation(Image.fromarray(img_gray))
    if osd_angle is not None:
        conf = _ocr_quick_conf(img_gray, lang=lang)
        return (osd_angle, conf)
    # 2) ensemble: try 0,90,180,270 and pick highest quick OCR conf
    best = (0, -1.0)
    for angle in (0, 90, 180, 270):
        M = cv2.getRotationMatrix2D((img_gray.shape[1]/2, img_gray.shape[0]/2), -angle, 1.0)
        rot = cv2.warpAffine(img_gray, M, (img_gray.shape[1], img_gray.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        conf = _ocr_quick_conf(rot, lang=lang)
        if conf > best[1]:
            best = (angle, conf)
    return best  # (angle, conf)

def rotate_image(img_bgr, angle: int):
    if angle % 360 == 0:
        return img_bgr
    (h, w) = img_bgr.shape[:2]
    M = cv2.getRotationMatrix2D((w/2, h/2), -angle, 1.0)
    return cv2.warpAffine(img_bgr, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

def preprocess_for_ocr(img_gray: np.ndarray) -> np.ndarray:
    # upscale + denoise + adaptive threshold (good general-purpose pipeline)
    h, w = img_gray.shape[:2]
    img = cv2.resize(img_gray, (w*UPSCALE, h*UPSCALE), interpolation=cv2.INTER_CUBIC)
    img = cv2.fastNlMeansDenoising(img, None, DENOISE_H, 7, 21)
    # adaptive threshold with slight blur to avoid over-sharp artifacts
    img = cv2.GaussianBlur(img, (3,3), 0)
    img = cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 11, 2)
    return img

def ocr_image(img_bgr, lang='eng', psm=6) -> Tuple[str, float, dict]:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    prep = preprocess_for_ocr(gray)
    pil = Image.fromarray(prep)
    config = f'--psm {psm} --oem 3'
    data = pytesseract.image_to_data(pil, lang=lang, config=config, output_type=Output.DICT)
    # build text and confidence
    texts = []
    confs = []
    for t, c in zip(data.get('text', []), data.get('conf', [])):
        if t and t.strip():
            texts.append(t.strip())
        try:
            ci = float(c)
            if ci >= 0:
                confs.append(ci)
        except Exception:
            pass
    text = " ".join(texts).strip()
    avg_conf = float(sum(confs))/len(confs) if confs else 0.0
    return text, avg_conf, data

# region extraction helpers (proportional boxes)
def extract_regions_mtg(img_bgr) -> Dict[str, np.ndarray]:
    h, w = img_bgr.shape[:2]
    # central suggests common MTG card layout: name at top, art below, oracle text bottom half
    regions = {}
    # name box ~ top 9-16% (tunable)
    regions['name'] = img_bgr[int(h*0.03):int(h*0.13), int(w*0.07):int(w*0.93)]
    # type line ~ just below art area top of bottom third
    regions['type_line'] = img_bgr[int(h*0.60):int(h*0.72), int(w*0.12):int(w*0.88)]
    # oracle / rules text area ~ lower 30-62%
    regions['oracle'] = img_bgr[int(h*0.72):int(h*0.95), int(w*0.08):int(w*0.92)]
    # collector number lower-right (small): crop small window bottom-right
    regions['collector'] = img_bgr[int(h*0.90):int(h*0.98), int(w*0.75):int(w*0.97)]
    return regions

# main processing entry
def process_card_image(path_or_array,
                       game: str = 'mtg',
                       lang: str = 'eng',
                       identifier_callback: Optional[Callable[[Dict[str,str]], Any]] = None
                       ) -> Dict[str, Any]:
    img = load_image(path_or_array)
    angle, quick_conf = detect_best_rotation(img, lang=lang)
    rotated = rotate_image(img, angle)
    # after rotation, optionally run a slight crop to remove black borders from rotation (skip for now)
    results = {
        'rotation_detected': int(angle),
        'rotation_confidence': float(quick_conf),
        'regions': {},
        'ocr': {},
    }
    # choose region extractor per game
    if game == 'mtg':
        regions = extract_regions_mtg(rotated)
    else:
        # fallback: whole-card OCR + center crop
        h, w = rotated.shape[:2]
        regions = {'full': rotated[int(h*0.05):int(h*0.95), int(w*0.05):int(w*0.95)],
                   'center': rotated[int(h*0.25):int(h*0.75), int(w*0.25):int(w*0.75)]}
    # OCR each region with tuned psm: name = single line, rest = automatic
    for key, crop in regions.items():
        if crop is None or crop.size == 0:
            continue
        psm = 7 if key == 'name' else 6
        text, conf, data = ocr_image(crop, lang=lang, psm=psm)
        results['regions'][key] = {
            'text': text,
            'confidence': conf,
            'box_shape': crop.shape[:2],
        }
        results['ocr'][key] = data
    # optionally call external identifier with a minimal payload
    if identifier_callback:
        # pass simple mapping of region->text
        text_map = {k: v['text'] for k, v in results['regions'].items()}
        try:
            id_result = identifier_callback(text_map)
            results['identifier'] = id_result
        except Exception as e:
            results['identifier_error'] = str(e)
    return results

# small convenience CLI when run directly for quick tests
if __name__ == '__main__':
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: ocr.py <image>")
        sys.exit(1)
    out = process_card_image(sys.argv[1], game='mtg')
    print(json.dumps(out, indent=2, default=str))