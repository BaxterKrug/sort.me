import os
from dataclasses import asdict
from typing import Optional, List, Dict, Any

from . import card_id, assign

def identify_and_assign(
    ocr_map: Dict[str, str],
    db_path: Optional[str],
    cards_list: Optional[List[Dict[str, Any]]],
    cfg: assign.Config,
    state: assign.SystemState,
    *,
    update_state: bool = True,
) -> Dict[str, Any]:
    """
    Take OCR region->text, identify the card against a local DB (or list),
    then wrap that result into an assign.Card and call assign.assign_card.

    Returns a dict with the assigned cell, reason, constructed card and
    the identification debug info.
    """
    # prefer using precomputed embeddings when available
    id_res = card_id.identify_card_from_ocr(
        ocr_map,
        db_path=db_path,
        cards_list=cards_list,
        embeddings_dir=os.path.join("data", "embeddings"),
    )

    # identification confidence -> 0.0..1.0
    id_score = float(id_res.get('score', 0.0))
    id_conf = min(1.0, id_score / 100.0)

    best = id_res.get('best') or {}
    # prefer canonical fields from the matched card, fallback to OCR text
    name = (best.get('name') or best.get('title') or ocr_map.get('name') or ocr_map.get('title') or "").strip()
    set_code = (best.get('set') or best.get('set_code') or ocr_map.get('set') or None)
    collector = (best.get('collector_number') or best.get('collector') or ocr_map.get('collector') or None)

    card = assign.Card(
        game='mtg',                # change per-game when needed
        name=name,
        set_code=set_code,
        collector_number=collector,
        scryfall_id=best.get('id') or best.get('scryfall_id'),
        confidence=float(id_conf),
        printed_name=best.get("printed_name"),
        flavor_name=best.get("flavor_name"),
    )

    cell, reason = assign.assign_card(card, cfg, state)

    if update_state and cell:
        try:
            state.counts_by_cell[cell] = state.counts_by_cell.get(cell, 0) + 1
        except Exception:
            pass

    return {
        'cell': cell,
        'reason': reason,
        'card': card,
        'card_dict': asdict(card),
        'identify': id_res,
        'ocr_fields': dict(ocr_map),
    }