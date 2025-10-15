from src.ocr import ocr_with_tesseract
import pytest


def test_ocr_missing_file():
    with pytest.raises(FileNotFoundError):
        ocr_with_tesseract('no-such-file.jpg')
