import numpy as np
import pytest

from app.services import ocr_pipeline


@pytest.fixture(autouse=True)
def reset_ocr_globals(monkeypatch):
    # Ensure each test starts from a neutral state
    monkeypatch.setattr(ocr_pipeline, "_OCR_ENGINE_WARNED", False, raising=False)
    monkeypatch.setattr(ocr_pipeline, "_EASYOCR_READER", None, raising=False)


def _dummy_frame() -> np.ndarray:
    return np.ones((120, 60), dtype=np.uint8)


def test_prepare_for_ocr_rotates_landscape():
    landscape = np.zeros((200, 400, 3), dtype=np.uint8)
    prepared, meta = ocr_pipeline._prepare_for_ocr(landscape)

    assert prepared.shape[0] > prepared.shape[1], "expected portrait-prepped image"
    assert meta["portrait_rotated"] is True
    assert meta["portrait_rotation"] == "rotate_90_ccw"
    assert meta["input_shape"] == [200, 400, 3]
    assert meta["portrait_shape"][0] > meta["portrait_shape"][1]


def test_perform_ocr_prefers_tesseract(monkeypatch):
    captured_regions = []

    class DummyTesseract:
        @staticmethod
        def image_to_string(img, config=None, lang=None):
            captured_regions.append((img.shape, config, lang))
            return "Lightning Bolt"

    def _fail_fallback(*_args, **_kwargs):  # pragma: no cover - defensive
        raise AssertionError("should not fallback")

    monkeypatch.setattr(ocr_pipeline, "HAVE_TESSERACT", True, raising=False)
    monkeypatch.setattr(ocr_pipeline, "pytesseract", DummyTesseract, raising=False)
    monkeypatch.setattr(ocr_pipeline, "HAVE_EASYOCR", False, raising=False)
    monkeypatch.setattr(ocr_pipeline, "_perform_easyocr", _fail_fallback, raising=False)

    ocr_map, meta = ocr_pipeline._perform_ocr(_dummy_frame())

    assert captured_regions, "expected tesseract to process regions"
    assert ocr_map["full"] == "Lightning Bolt"
    assert meta["engine"] == "tesseract"


def test_perform_ocr_falls_back_to_easyocr(monkeypatch):
    fallback_called = {}

    def fake_easyocr(image, regions):
        fallback_called["called"] = True
        text_map = {"full": "", "name": "", "oracle": "Shock", "collector": ""}
        text_map["rules"] = text_map["oracle"]
        text_map["full_text"] = text_map["oracle"]
        return text_map, {"engine": "easyocr", "duration_ms": 7, "error": None}

    monkeypatch.setattr(ocr_pipeline, "HAVE_TESSERACT", False, raising=False)
    monkeypatch.setattr(ocr_pipeline, "pytesseract", None, raising=False)
    monkeypatch.setattr(ocr_pipeline, "HAVE_EASYOCR", True, raising=False)
    monkeypatch.setattr(ocr_pipeline, "_perform_easyocr", fake_easyocr, raising=False)

    ocr_map, meta = ocr_pipeline._perform_ocr(_dummy_frame())

    assert fallback_called.get("called"), "expected easyocr fallback"
    assert ocr_map["oracle"] == "Shock"
    assert meta["engine"] == "easyocr"
    assert meta.get("fallback_engine") == "easyocr"


def test_perform_ocr_applies_text_cleaning(monkeypatch):
    class DummyTesseract:
        @staticmethod
        def image_to_string(img, config=None, lang=None):  # pragma: no cover - arguments unused
            return "  “Rakshasa -- Debaser”  012 / 345  "

    monkeypatch.setattr(ocr_pipeline, "HAVE_TESSERACT", True, raising=False)
    monkeypatch.setattr(ocr_pipeline, "pytesseract", DummyTesseract, raising=False)
    monkeypatch.setattr(ocr_pipeline, "HAVE_EASYOCR", False, raising=False)

    ocr_map, _ = ocr_pipeline._perform_ocr(_dummy_frame())

    assert ocr_map["name"] == "Rakshasa Debaser"
    assert ocr_map["collector"] == "012/345"
    assert "\n" not in ocr_map["full_text"]
    assert ocr_map["rules"] == ocr_map["oracle"]


def test_run_ocr_from_bytes_uses_helpers(monkeypatch):
    class Marker:
        shape = (4, 4)

    decoded_marker = Marker()

    def fake_decode(data, flag):
        assert isinstance(data, bytes)
        return decoded_marker

    def fake_run(image):
        assert image is decoded_marker
        return ({"full": "Island"}, {"engine": "test"})

    monkeypatch.setattr(ocr_pipeline, "_decode_image_bytes", fake_decode)
    monkeypatch.setattr(ocr_pipeline, "run_ocr_from_image", fake_run)

    result = ocr_pipeline.run_ocr_from_bytes(b"bytes")
    assert result["ocr_map"]["full"] == "Island"
    assert result["ocr_meta"]["engine"] == "test"
    assert result["ocr_meta"]["input_shape"] == [4, 4]


def test_run_ocr_from_path_reads_file(tmp_path, monkeypatch):
    sample = tmp_path / "ocr.png"
    sample.write_bytes(b"abc123")
    captured = {}

    def fake_from_bytes(data):
        captured["data"] = data
        return {"ocr_map": {"full": "Voyage's End"}, "ocr_meta": {"engine": "fake"}}

    monkeypatch.setattr(ocr_pipeline, "run_ocr_from_bytes", fake_from_bytes)

    result = ocr_pipeline.run_ocr_from_path(sample)
    assert captured["data"] == b"abc123"
    assert result["ocr_map"]["full"] == "Voyage's End"
