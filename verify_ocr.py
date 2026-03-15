#!/usr/bin/env python3
"""Quick verification that OCR pipeline is functional."""

import sys
from app.services import ocr_pipeline

def main():
    print("=" * 60)
    print("OCR Setup Verification")
    print("=" * 60)
    
    # Check Tesseract availability
    if ocr_pipeline.HAVE_TESSERACT:
        print(f"✓ Tesseract OCR: AVAILABLE")
        print(f"  Path: {ocr_pipeline._TESSERACT_PATH}")
    else:
        print("✗ Tesseract OCR: NOT AVAILABLE")
    
    # Check EasyOCR availability
    if ocr_pipeline.HAVE_EASYOCR:
        print(f"✓ EasyOCR: AVAILABLE")
    else:
        print("✗ EasyOCR: NOT AVAILABLE (optional fallback)")
    
    print("\n" + "=" * 60)
    
    # Determine OCR engine status
    if ocr_pipeline.HAVE_TESSERACT:
        print("✓ PRIMARY OCR ENGINE: Tesseract (fast, reliable)")
        print("  Status: READY FOR CARD SCANNING")
        return 0
    elif ocr_pipeline.HAVE_EASYOCR:
        print("✓ FALLBACK OCR ENGINE: EasyOCR (slower, but functional)")
        print("  Status: READY FOR CARD SCANNING")
        return 0
    else:
        print("✗ NO OCR ENGINE AVAILABLE")
        print("  Status: NOT READY - Install Tesseract or EasyOCR")
        return 1

if __name__ == "__main__":
    sys.exit(main())
