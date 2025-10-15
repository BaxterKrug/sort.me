#!/usr/bin/env python3
"""Process images in data/sample_images: run OCR and compute embeddings.

Saves JSON results to data/processed_results.json and embeddings to data/embeddings/. Handles missing optional deps gracefully.
Run with: python scripts/process_samples.py
"""
import os
import sys
import json
from pathlib import Path

# Ensure project root is on sys.path so `from src...` imports work when running
# this script directly (e.g., `python scripts/process_samples.py`).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in map(str, sys.path):
    sys.path.insert(0, str(ROOT))


SAMPLES = ROOT / 'data' / 'sample_images'
OUT_JSON = ROOT / 'data' / 'processed_results.json'
EMB_DIR = ROOT / 'data' / 'embeddings'

OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
EMB_DIR.mkdir(parents=True, exist_ok=True)

def main():
    files = sorted([p for p in SAMPLES.iterdir() if p.suffix.lower() in ('.jpg','.jpeg','.png')])
    if not files:
        print('No sample images found in', SAMPLES)
        return

    results = []
    for p in files:
        print('Processing', p.name)
        item = {'file': str(p.name)}
        # OCR
        try:
            from src.ocr import ocr_with_easyocr, ocr_with_tesseract
            try:
                text = ocr_with_easyocr(str(p))
            except Exception:
                text = ocr_with_tesseract(str(p))
            item['ocr'] = text
        except Exception as e:
            item['ocr_error'] = repr(e)

        # Embedding
        try:
            from src.embeddings import SimpleEmbedder
            embder = SimpleEmbedder(device='cpu')
            emb = embder.embed(str(p))
            # save embedding as numpy file
            try:
                import numpy as _np
                _np.save(EMB_DIR / (p.stem + '.npy'), emb)
                item['embedding_file'] = str((EMB_DIR / (p.stem + '.npy')).name)
            except Exception:
                # fallback: store first 10 dims inline
                item['embedding_preview'] = list(map(float, emb[:10]))
        except Exception as e:
            item['embedding_error'] = repr(e)

        results.append(item)

    with open(OUT_JSON, 'w', encoding='utf8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print('\nDone. Results written to', OUT_JSON)


if __name__ == '__main__':
    main()
