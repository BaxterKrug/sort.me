from typing import Optional, List, Dict, Any
from . import card_id, assign

def identify_and_assign(ocr_map: Dict[str, str],
                        db_path: Optional[str],
                        cards_list: Optional[List[Dict[str,Any]]],
                        cfg: assign.Config,
                        state: assign.SystemState
                        ) -> Dict[str, Any]:
    """
    Take OCR region->text, identify the card against a local DB (or list),
    then wrap that result into an assign.Card and call assign.assign_card.

    Returns a dict with the assigned cell, reason, constructed card and
    the identification debug info.
    """
    id_res = card_id.identify_card_from_ocr(ocr_map, db_path=db_path, cards_list=cards_list)

    # identification confidence -> 0.0..1.0
    id_score = float(id_res.get('score', 0.0))
    id_conf = min(1.0, id_score / 100.0)

    best = id_res.get('best') or {}
    # prefer canonical fields from the matched card, fallback to OCR text
    name = (best.get('name') or best.get('title') or ocr_map.get('name') or ocr_map.get('title') or "").strip()
    set_code = (best.get('set') or best.get('set_code') or ocr_map.get('set') or None)
    collector = (best.get('collector_number') or best.get('collector') or ocr_map.get('collector') or None)

    card = assign.Card(
        game = 'mtg',                # change per-game when needed
        name = name,
        set_code = set_code,
        collector_number = collector,
        confidence = float(id_conf)
    )

    cell, reason = assign.assign_card(card, cfg, state)

    return {
        'cell': cell,
        'reason': reason,
        'card': card,
        'identify': id_res
    }