# app/main.py (or similar)
import os
from typing import List, Optional

import cv2
import numpy as np
import yaml
from fastapi import File, Form, HTTPException, UploadFile
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.services import card_id, ocr
from app.services.assign import Card, SystemState, assign_card, load_config

app = FastAPI()

# Serve the single-page UI and static assets from the `app/static/` folder
# - GET / will return app/static/index.html
# - static assets (JS/CSS) will be available under /static/
static_dir = os.path.join("app", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def read_index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    # If index.html is missing, return a small JSON explaining the issue
    raise HTTPException(status_code=404, detail="Web UI not found. Ensure app/static/index.html exists.")

CFG = load_config(yaml.safe_load(open("config.yaml")))
STATE = SystemState(counts_by_cell={cid: 0 for cid in CFG.cells})


def _default_card_db_path() -> Optional[str]:
    """Return the default card database path if available."""
    env_path = os.environ.get("SORTME_CARD_DB_PATH")
    if env_path:
        env_path = os.path.expanduser(env_path)
        if os.path.exists(env_path):
            return env_path
    local_path = os.path.join("data", "demo_cards.json")
    if os.path.exists(local_path):
        return local_path
    return None


_CARD_DB_CACHE: Optional[List[dict]] = None
_CARD_DB_PATH: Optional[str] = None


def _load_card_db(path: str) -> List[dict]:
    """Load and cache the local card DB to avoid repeated disk hits."""

    global _CARD_DB_CACHE, _CARD_DB_PATH

    if not path:
        raise ValueError("Card database path is required")

    path = os.path.expanduser(path)
    if _CARD_DB_CACHE is None or _CARD_DB_PATH != path:
        cards = card_id.load_local_db(path)
        _CARD_DB_CACHE = cards
        _CARD_DB_PATH = path
    return _CARD_DB_CACHE

@app.get("/debug/alpha_map")
def alpha_map():
    return {"letter_to_cell": CFG.letter_to_cell}

@app.post("/debug/reset_counts")
def reset_counts():
    for k in STATE.counts_by_cell.keys():
        STATE.counts_by_cell[k] = 0
    return {"ok": True}

@app.post("/debug/assign")
def debug_assign(payload: dict):
    name = str(payload.get("name","")).strip()
    conf = float(payload.get("confidence", 1.0))
    card = Card(game=payload.get("game","mtg"), name=name, confidence=conf)
    cell, reason = assign_card(card, CFG, STATE)
    STATE.counts_by_cell[cell] = STATE.counts_by_cell.get(cell, 0) + 1
    return {"cell": cell, "reason": reason, "counts": STATE.counts_by_cell}

# Non-mutating preview endpoint for the UI assignment preview
@app.post("/debug/assign_preview")
def debug_assign_preview(payload: dict):
    name = str(payload.get("name","")).strip()
    conf = float(payload.get("confidence", 1.0))
    card = Card(game=payload.get("game","mtg"), name=name, confidence=conf)
    # reuse same assignment logic but DO NOT increment STATE
    cell, reason = assign_card(card, CFG, STATE)
    first = (name[:1].upper() if name and name[0].isalpha() else "A")
    return {"cell": cell, "reason": reason, "first": first}


@app.post("/demo/batch_identify")
async def demo_batch_identify(
    files: List[UploadFile] = File(...),
    db_path: Optional[str] = Form(None),
    use_filename_expected: bool = Form(True),
    ocr_only: bool = Form(False),
):
    """
    Run a batch OCR + identification pass for uploaded images.

    Expected usage (for quick demo workflows):
      - Upload multiple card images.
      - Optionally use the filename (e.g. "Lightning Bolt__B1.jpg") to
        supply the expected card name and/or cell.
      - Returns per-image OCR details, identification guesses, assignments,
        and aggregate accuracy stats.
    """

    if not files:
        raise HTTPException(status_code=400, detail="No images uploaded")

    active_db_path = db_path or _default_card_db_path()
    cards_db = None
    # Try to load a card DB if a path is provided; otherwise allow OCR-only operation
    if active_db_path:
        try:
            cards_db = _load_card_db(active_db_path)
        except Exception as exc:  # pragma: no cover - defensive guard
            raise HTTPException(status_code=400, detail=f"Failed to load card DB: {exc}")

    results = []
    name_matches = 0
    cell_matches = 0
    both_matches = 0

    # local state snapshot so we don't mutate live counts
    state_snapshot = SystemState(counts_by_cell=dict(STATE.counts_by_cell))

    for idx, upload in enumerate(files, start=1):
        file_result = {
            "index": idx,
            "filename": upload.filename,
        }

        try:
            raw = await upload.read()
            if not raw:
                raise ValueError("Empty file")

            buffer = np.frombuffer(raw, dtype=np.uint8)
            img = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("Unsupported image format")

            ocr_res = ocr.process_card_image(img, game="mtg")
            regions = ocr_res.get("regions", {})
            region_texts = {key: (val.get("text", "") if isinstance(val, dict) else "") for key, val in regions.items()}

            # If ocr_only flag present, skip identification and assignment and return simplified OCR-only data
            if ocr_only:
                # Build a single aggregated text string from the region_texts (preserve readable order if available)
                ordered_keys = [k for k in ['name','type_line','oracle','collector','full'] if k in region_texts]
                # append any other keys in their existing order
                ordered_keys += [k for k in region_texts.keys() if k not in ordered_keys]
                parts = [region_texts.get(k) for k in ordered_keys if region_texts.get(k)]
                aggregated = "\n".join(parts) if parts else ""

                # Return only filename and the aggregated OCR text and the simple per-region strings
                file_result.update({
                    "ocr_text": aggregated,
                    "region_texts": region_texts,  # simple map of region -> text (strings only)
                })
                results.append(file_result)
                continue

            # If a cards DB is available, run identification. If not, but precomputed embeddings exist,
            # still run identification using the embeddings-only path.
            embeddings_dir = os.path.join("data", "embeddings")
            has_embeddings = os.path.exists(os.path.join(embeddings_dir, 'embeddings.npy')) and os.path.exists(os.path.join(embeddings_dir, 'cards_metadata.json'))

            if cards_db or has_embeddings:
                identify_res = card_id.identify_card_from_ocr(
                    region_texts,
                    cards_list=cards_db if cards_db else None,
                    embeddings_dir=embeddings_dir if has_embeddings else None,
                )
                best = identify_res.get("best") or {}
                identified_name = (best.get("name") or best.get("title") or region_texts.get("name") or "").strip()
                id_score = float(identify_res.get("score", 0.0))
            else:
                identify_res = {}
                best = {}
                identified_name = (region_texts.get("name") or "").strip()
                id_score = 0.0

            card_conf = min(1.0, id_score / 100.0) if id_score > 0 else 0.0

            card = Card(
                game="mtg",
                name=identified_name,
                set_code=(best.get("set") or best.get("set_code")),
                collector_number=(best.get("collector_number") or best.get("collector")),
                confidence=card_conf,
            )

            cell, reason = assign_card(card, CFG, state_snapshot)

            expected_name = None
            expected_cell = None
            if use_filename_expected and upload.filename:
                base = os.path.splitext(os.path.basename(upload.filename))[0]
                if "__" in base:
                    parts = base.split("__", 1)
                    expected_name = parts[0].replace("_", " ").strip()
                    expected_cell = parts[1].strip().upper() or None
                else:
                    expected_name = base.replace("_", " ").strip()

            if expected_cell is None and expected_name:
                tmp_card = Card(game="mtg", name=expected_name, confidence=1.0)
                expected_cell, _ = assign_card(tmp_card, CFG, state_snapshot)

            match_name = False
            if expected_name and identified_name:
                match_name = expected_name.lower() == identified_name.lower()

            match_cell = False
            if expected_cell and cell:
                match_cell = expected_cell.upper() == cell.upper()

            if match_name:
                name_matches += 1
            if match_cell:
                cell_matches += 1
            if match_name and match_cell:
                both_matches += 1

            file_result.update(
                {
                    "expected": {
                        "name": expected_name,
                        "cell": expected_cell,
                    },
                    "ocr": {
                        "rotation": ocr_res.get("rotation_detected"),
                        "rotation_confidence": ocr_res.get("rotation_confidence"),
                        "regions": regions,
                    },
                    "region_texts": region_texts,
                    "identify": identify_res,
                    "identify_debug": identify_res.get("debug"),
                    "identified_name": identified_name,
                    "id_score": id_score,
                    "assignment": {
                        "cell": cell,
                        "reason": reason,
                    },
                    "match_name": match_name,
                    "match_cell": match_cell,
                }
            )

        except Exception as exc:
            file_result.update({"error": str(exc)})

        results.append(file_result)

    summary = {
        "total": len(results),
        "db_path": active_db_path,
        "name_matches": name_matches,
        "cell_matches": cell_matches,
        "both_matches": both_matches,
    }

    return {
        "summary": summary,
        "results": results,
    }
