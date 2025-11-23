#!/usr/bin/env python3
"""Utility script to composite snapshot pairs and generate OCR-ready images."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

import cv2  # type: ignore[import-not-found]

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import ocr_pipeline  # pylint: disable=wrong-import-position


def _load_frame(path: Path) -> Any:
    frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError(f"Unable to load image: {path}")
    return frame


def _build_result_payload(artifacts: Dict[str, Any], timestamp_slug: str) -> Dict[str, Any]:
    result = {
        "timestamp_slug": artifacts.get("meta", {}).get("timestamp_slug", timestamp_slug),
        "paths": {
            "top": artifacts.get("top", {}).get("path"),
            "bottom": artifacts.get("bottom", {}).get("path"),
            "composite": artifacts.get("composite", {}).get("path"),
            "ocr": artifacts.get("ocr_prepared", {}).get("path"),
        },
        "meta": artifacts.get("meta", {}),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Composite snapshot pair and prepare OCR-ready output.")
    parser.add_argument("--top", required=True, help="Path to the top snapshot image")
    parser.add_argument("--bottom", required=True, help="Path to the bottom snapshot image")
    parser.add_argument(
        "--output-dir",
        default="data/snapshots",
        help="Directory where composite outputs should be stored (default: %(default)s)",
    )
    parser.add_argument("--quality", type=int, default=92, help="JPEG quality for saved images (default: %(default)s)")
    parser.add_argument("--tag", help="Optional slug to use instead of the current timestamp")
    parser.add_argument("--no-save", action="store_true", help="Skip writing files; just emit metadata")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print metadata as JSON")
    args = parser.parse_args()

    top_path = Path(args.top)
    bottom_path = Path(args.bottom)
    if not top_path.exists():
        parser.error(f"Top snapshot not found: {top_path}")
    if not bottom_path.exists():
        parser.error(f"Bottom snapshot not found: {bottom_path}")

    frame_top = _load_frame(top_path)
    frame_bottom = _load_frame(bottom_path)

    timestamp_slug = args.tag or datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
    artifacts = ocr_pipeline.prepare_snapshot_artifacts(
        frame_top,
        frame_bottom,
        timestamp_slug=timestamp_slug,
        save_dir=Path(args.output_dir),
        jpeg_quality=args.quality,
        persist=not args.no_save,
        include_bytes=True,
    )

    # Drop raw bytes from the payload before presenting results
    for label in ("top", "bottom", "composite", "ocr_prepared"):
        if label in artifacts:
            artifacts[label].pop("bytes", None)

    result = _build_result_payload(artifacts, timestamp_slug)

    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Composite timestamp: {result['timestamp_slug']}")
        for key, value in result["paths"].items():
            print(f"{key.title()} path: {value or 'not saved'}")
        orientation = result["meta"].get("orientation", {})
        if orientation:
            print("Orientation:", json.dumps(orientation, indent=2))
        pair_orientation = result["meta"].get("pair_orientation", {})
        if pair_orientation:
            print("Pair orientation hint:", json.dumps(pair_orientation, indent=2))
        ocr_meta = result["meta"].get("ocr", {})
        if ocr_meta:
            print("OCR prep:", json.dumps(ocr_meta, indent=2))
if __name__ == "__main__":
    main()
