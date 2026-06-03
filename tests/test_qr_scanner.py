import asyncio

import numpy as np

from app.services import qr_scanner


class _StubDetector:
    def __init__(self) -> None:
        self.shapes = []

    def detectAndDecode(self, image):
        self.shapes.append(tuple(image.shape[:2]))
        h, w = image.shape[:2]
        if h >= 80 and w >= 80:
            corners = np.array([[[20.0, 20.0], [60.0, 20.0], [60.0, 60.0], [20.0, 60.0]]], dtype=np.float32)
            return "FEEDER_A1_END", corners, None
        return "", None, None


def test_detect_qr_retries_with_upscale():
    scanner = qr_scanner.QRScanner(history_length=1, enabled=True)
    stub = _StubDetector()
    scanner._qr_detector = stub

    frame = np.zeros((40, 40, 3), dtype=np.uint8)
    detected, data, corners = scanner._detect_qr(frame)

    assert detected is True
    assert data == "FEEDER_A1_END"
    assert corners is not None
    assert stub.shapes[0] == (40, 40)
    assert any(h > 40 and w > 40 for h, w in stub.shapes)


def test_scan_accepts_target_cells_argument():
    scanner = qr_scanner.QRScanner(history_length=1, enabled=True)
    scanner._detect_qr = lambda frame: (True, "FEEDER_A1_END", None)

    result = asyncio.run(scanner.scan(frame=np.zeros((16, 16, 3), dtype=np.uint8), target_cells=["A1"]))

    assert result["detected"] is True
    assert result["stable"] is True
    assert result["cell"] == "A1"
