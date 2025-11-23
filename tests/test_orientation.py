import numpy as np
import cv2

from app.services import ocr_pipeline


def _build_mock_card(height: int = 640, width: int = 430) -> np.ndarray:
    image = np.full((height, width, 3), 70, dtype=np.uint8)

    header_height = int(height * 0.14)
    header_start = 10
    header_end = header_start + header_height
    cv2.rectangle(image, (16, header_start), (width - 16, header_end), (35, 35, 35), -1)

    for idx in range(4):
        y = header_start + 10 + idx * 6
        cv2.line(image, (24, y), (width - 24, y), (210, 210, 210), 1)

    art_start = header_end + 8
    art_end = art_start + int(height * 0.35)
    for row in range(art_start, art_end, 3):
        color = 60 + (row - art_start) % 90
        cv2.line(image, (20, row), (width - 20, row), (color, color - 10, color + 20), 1)

    text_start = art_end + 16
    for idx in range(10):
        y = text_start + idx * 12
        cv2.line(image, (24, y), (width - 24, y), (200, 200, 200), 1)

    return image


def _build_mask(height: int, width: int) -> np.ndarray:
    mask = np.linspace(0.0, 1.0, height, dtype=np.float32).reshape(-1, 1)
    return np.tile(mask, (1, width))


def _build_tilted_scene(angle: float = 17.0) -> tuple[np.ndarray, np.ndarray]:
    card = _build_mock_card()
    pad_h = card.shape[0] + 300
    pad_w = card.shape[1] + 300
    canvas = np.full((pad_h, pad_w, 3), 25, dtype=np.uint8)
    offset_y = (pad_h - card.shape[0]) // 2
    offset_x = (pad_w - card.shape[1]) // 2
    canvas[offset_y : offset_y + card.shape[0], offset_x : offset_x + card.shape[1]] = card

    mask = np.zeros((pad_h, pad_w), dtype=np.float32)
    mask[offset_y : offset_y + card.shape[0], offset_x : offset_x + card.shape[1]] = _build_mask(card.shape[0], card.shape[1])

    center = (pad_w // 2, pad_h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(canvas, matrix, (pad_w, pad_h), borderValue=(20, 20, 20))
    rotated_mask = cv2.warpAffine(mask, matrix, (pad_w, pad_h), flags=cv2.INTER_LINEAR)
    return rotated, rotated_mask


def test_orient_composite_rotates_clockwise_when_needed():
    card = _build_mock_card()
    mask = _build_mask(*card.shape[:2])
    sideways = cv2.rotate(card, cv2.ROTATE_90_COUNTERCLOCKWISE)
    mask_sideways = cv2.rotate(mask, cv2.ROTATE_90_COUNTERCLOCKWISE)

    oriented, meta = ocr_pipeline._orient_composite(sideways.copy(), mask_sideways)

    assert meta["rotation_direction"] == "clockwise"
    np.testing.assert_array_equal(oriented, card)


def test_orient_composite_rotates_counterclockwise_when_needed():
    card = _build_mock_card()
    mask = _build_mask(*card.shape[:2])
    sideways = cv2.rotate(card, cv2.ROTATE_90_CLOCKWISE)
    mask_sideways = cv2.rotate(mask, cv2.ROTATE_90_CLOCKWISE)

    oriented, meta = ocr_pipeline._orient_composite(sideways.copy(), mask_sideways)

    assert meta["rotation_direction"] == "counterclockwise"
    np.testing.assert_array_equal(oriented, card)


def test_prepare_snapshot_artifacts_surface_rotated_and_aligned(tmp_path, monkeypatch):
    card = _build_mock_card()
    shifted = np.roll(card, 20, axis=0)

    fake_embedding = {
        "query": "Lightning Bolt",
        "matches": [
            {
                "card": {"name": "Lightning Bolt", "set": "m11", "collector_number": "150"},
                "score": 88.5,
                "distance": 0.12,
            }
        ],
        "best": {
            "card": {"name": "Lightning Bolt", "set": "m11", "collector_number": "150"},
            "score": 88.5,
            "distance": 0.12,
        },
        "engine": "test-model",
        "error": None,
        "available": True,
    }

    monkeypatch.setattr(ocr_pipeline.card_id, "embedding_matches_from_ocr", lambda *args, **kwargs: fake_embedding)

    def fake_perform_ocr(_image):
        return ({"full": "Bolt", "name": "Lightning Bolt", "oracle": "Deal 3 damage.", "collector": "150"}, {"engine": "mock", "duration_ms": 1})

    monkeypatch.setattr(ocr_pipeline, "_perform_ocr", fake_perform_ocr)

    artifacts = ocr_pipeline.prepare_snapshot_artifacts(
        card,
        shifted,
        timestamp_slug="unit-test",
        save_dir=tmp_path,
        persist=False,
        include_bytes=False,
    )

    assert "composite_rotated" in artifacts
    assert "composite_aligned" in artifacts
    assert "composite_raw" in artifacts

    rotated_shape = artifacts["composite_rotated"]["shape"]
    aligned_shape = artifacts["composite_aligned"]["shape"]
    raw_shape = artifacts["composite_raw"]["shape"]

    assert raw_shape[0] >= aligned_shape[0]
    assert aligned_shape[0] >= aligned_shape[1]
    assert rotated_shape == artifacts["composite"]["shape"]

    border_meta = artifacts["composite_aligned"]["meta"].get("border", {})
    assert "area" in border_meta
    assert border_meta.get("margin_px") == 20

    meta = artifacts.get("meta", {})
    ocr_map = meta.get("ocr_map") or {}
    assert set(ocr_map.keys()).issuperset({"full", "name", "oracle", "collector"})
    ocr_result = meta.get("ocr_result") or {}
    assert "engine" in ocr_result
    assert meta.get("embedding") == fake_embedding
    assert ocr_result.get("embedding") == fake_embedding

    ocr_asset = artifacts.get("ocr_prepared") or {}
    ocr_meta = ocr_asset.get("meta") or {}
    assert ocr_meta.get("source") == "composite_rotated"


def test_warp_card_to_bounds_aligns_portrait():
    skewed, mask = _build_tilted_scene()
    warped, warped_mask, meta = ocr_pipeline._warp_card_to_bounds(skewed, mask)

    assert meta["found"] is True
    assert meta.get("margin_px") == 20
    assert warped.shape[0] > warped.shape[1]
    base_height = _build_mock_card().shape[0]
    assert warped.shape[0] >= base_height + 30
    assert warped_mask.shape[:2] == warped.shape[:2]


def test_text_band_features_detect_gap():
    card = _build_mock_card()
    features = ocr_pipeline._text_band_features(card)
    assert features["gap_ok"] is True
    assert features["score"] > 0
