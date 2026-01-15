"""
Lightweight card identification without text embeddings.

Given OCR region->text (e.g. {'name': "...", 'oracle': "...", 'collector': "12/264", ...})
attempt to find the best matching card from a local card database using:
- Exact normalized name matching
- Collector number + set code matching
- Fuzzy name matching (rapidfuzz or difflib)
- Oracle text token overlap
- Type line similarity
- Multi-field weighted scoring

Database loader supports:
 - JSON file containing a list of card objects
 - NDJSON (one JSON object per line)
 - SQLite DB with a 'cards' table

No embedding models or ML dependencies required - just string matching and basic statistics.
"""

from typing import Dict, Any, List, Optional, Tuple
from . import text_clean
import json
import os
import sqlite3
import unicodedata
import re
import logging
from pathlib import Path

LOG = logging.getLogger("sort.card_id")

# try to use rapidfuzz for better fuzzy matching, otherwise fallback
try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except Exception:
    HAVE_RAPIDFUZZ = False
    import difflib


# ------ helpers ------

def _normalize(s: Optional[str]) -> str:
    """Normalize string for comparison: lowercase, remove diacritics and punctuation."""
    if not s:
        return ""
    s = str(s)
    # remove diacritics, lowercase, remove punctuation, collapse whitespace
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokenize(s: str) -> set:
    """Split string into normalized tokens."""
    return set([t for t in re.split(r"\W+", _normalize(s)) if t and len(t) > 1])


def load_local_db(path: str) -> List[Dict[str, Any]]:
    """
    Load local db from JSON/NDJSON or SQLite file. Returns a list of card dicts.
    Expected minimal keys per card: 'name' and ideally 'oracle_text'/'collector_number'/'set'
    """
    if not path:
        return []
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    
    if path.endswith(".json"):
        with open(path, "r", encoding="utf8") as fh:
            data = json.load(fh)
            if isinstance(data, dict) and "data" in data:
                # some exports (scryfall) wrap list in "data"
                data = data["data"]
            return data
    
    if path.endswith(".ndjson") or path.endswith(".ndjsonl") or path.endswith(".ndjsonl.txt"):
        out = []
        with open(path, "r", encoding="utf8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out
    
    # try sqlite
    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        # try common column names
        for colset in (("name","oracle_text","collector_number","set","id"),
                       ("name","oracle","collector_number","set_code","id"),
                       ("name","oracle_text","collector","set_code","id")):
            try:
                col_list = ", ".join(col for col in colset if col)
                cur.execute(f"SELECT {col_list} FROM cards LIMIT 1")
                rows = cur.fetchall()
                # if query succeeded, fetch all rows
                cur.execute(f"SELECT {col_list} FROM cards")
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                out = []
                for r in rows:
                    out.append({cols[i]: r[i] for i in range(len(cols))})
                conn.close()
                return out
            except Exception:
                continue
        conn.close()
    except Exception:
        pass
    
    # fallback: try to read as JSON anyway
    with open(path, "r", encoding="utf8") as fh:
        try:
            return json.load(fh)
        except Exception:
            raise RuntimeError("Unsupported DB format or no cards found")


# ------ matching strategies ------

def _exact_name_match(ocr_name: str, cards: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find card with exact normalized name match."""
    if not ocr_name:
        return None
    norm_name = _normalize(ocr_name)
    for card in cards:
        card_name = _normalize(card.get("name") or card.get("title") or "")
        if card_name == norm_name:
            return card
    return None


def _collector_set_match(ocr_collector: str, ocr_set: str, cards: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find card by collector number and optionally set code."""
    if not ocr_collector:
        return None
    
    ocr_collector_norm = text_clean.normalize_collector(ocr_collector)
    ocr_set_norm = _normalize(ocr_set) if ocr_set else ""
    
    best_match = None
    best_score = 0
    
    for card in cards:
        card_collector = text_clean.normalize_collector(
            card.get("collector_number") or card.get("collector") or ""
        )
        if not card_collector or card_collector != ocr_collector_norm:
            continue
        
        # Found matching collector number
        if not ocr_set_norm:
            # No set to compare, this is a good match
            return card
        
        card_set = _normalize(card.get("set") or card.get("set_code") or "")
        if card_set == ocr_set_norm:
            # Perfect match: collector + set
            return card
        
        # Collector matches but set doesn't - keep as fallback
        if best_match is None:
            best_match = card
            best_score = 1
    
    return best_match


def _fuzzy_name_matches(ocr_name: str, cards: List[Dict[str, Any]], top_n: int = 10) -> List[Tuple[Dict[str, Any], float]]:
    """
    Return up to top_n candidate cards with fuzzy name similarity score (0..100).
    """
    if not ocr_name:
        return []
    
    norm_name = _normalize(ocr_name)
    
    # Build name list and mapping
    name_map = {}
    names = []
    for c in cards:
        n = _normalize(c.get("name") or c.get("title") or "")
        if not n:
            continue
        names.append(n)
        name_map[n] = name_map.get(n, []) + [c]
    
    if HAVE_RAPIDFUZZ:
        # Use rapidfuzz for better fuzzy matching
        choices = list(set(names))
        matches = rf_process.extract(norm_name, choices, scorer=rf_fuzz.WRatio, limit=top_n)
        out = []
        for match, score, _ in matches:
            for card in name_map.get(match, []):
                out.append((card, float(score)))
        return out
    else:
        # difflib fallback
        choices = list(set(names))
        matches = difflib.get_close_matches(norm_name, choices, n=top_n, cutoff=0.0)
        out = []
        for m in matches:
            # approximate score with SequenceMatcher ratio *100
            score = int(difflib.SequenceMatcher(None, norm_name, m).ratio() * 100)
            for card in name_map.get(m, []):
                out.append((card, float(score)))
        return out


def _oracle_similarity(ocr_oracle: str, card_oracle: str) -> float:
    """
    Compute token overlap score (0..100) between OCR oracle text and card oracle text.
    Uses Jaccard-like similarity weighted towards OCR tokens.
    """
    if not ocr_oracle or not card_oracle:
        return 0.0
    
    ocr_tokens = _tokenize(ocr_oracle)
    card_tokens = _tokenize(card_oracle)
    
    if not ocr_tokens or not card_tokens:
        return 0.0
    
    intersection = ocr_tokens.intersection(card_tokens)
    
    # Weight towards OCR tokens being found in card
    # (we want most OCR tokens found in the card)
    score = len(intersection) / len(ocr_tokens)
    return float(score * 100)


def _type_similarity(ocr_type: str, card_type: str) -> float:
    """
    Compute type line similarity (0..100).
    """
    if not ocr_type or not card_type:
        return 0.0
    
    ocr_tokens = _tokenize(ocr_type)
    card_tokens = _tokenize(card_type)
    
    if not ocr_tokens or not card_tokens:
        return 0.0
    
    intersection = ocr_tokens.intersection(card_tokens)
    union = ocr_tokens.union(card_tokens)
    
    # Jaccard similarity
    score = len(intersection) / len(union)
    return float(score * 100)


def _set_similarity(ocr_set: str, card_set: str) -> float:
    """Check if set codes match (0 or 100)."""
    if not ocr_set or not card_set:
        return 0.0
    
    ocr_norm = _normalize(ocr_set)
    card_norm = _normalize(card_set)
    
    return 100.0 if ocr_norm == card_norm else 0.0


def _collector_similarity(ocr_collector: str, card_collector: str) -> float:
    """Check if collector numbers match (0 or 100)."""
    if not ocr_collector or not card_collector:
        return 0.0
    
    ocr_norm = text_clean.normalize_collector(ocr_collector)
    card_norm = text_clean.normalize_collector(card_collector)
    
    return 100.0 if ocr_norm == card_norm else 0.0


def _score_candidate(ocr_map: Dict[str, str], card: Dict[str, Any], weights: Dict[str, float]) -> Dict[str, Any]:
    """
    Score a candidate card against OCR data using multiple criteria.
    Returns dict with individual scores and weighted total.
    """
    # Extract OCR data
    ocr_name = (ocr_map.get("name") or ocr_map.get("title") or "").strip()
    ocr_oracle = (ocr_map.get("oracle") or ocr_map.get("rules") or "").strip()
    ocr_type = (ocr_map.get("type") or ocr_map.get("type_line") or "").strip()
    ocr_set = (ocr_map.get("set") or ocr_map.get("set_code") or "").strip()
    ocr_collector = (ocr_map.get("collector") or ocr_map.get("collector_number") or "").strip()
    
    # Extract card data
    card_name = (card.get("name") or card.get("title") or "").strip()
    card_oracle = (card.get("oracle_text") or card.get("oracle") or "").strip()
    card_type = (card.get("type_line") or card.get("type") or "").strip()
    card_set = (card.get("set") or card.get("set_code") or "").strip()
    card_collector = (card.get("collector_number") or card.get("collector") or "").strip()
    
    # Calculate individual scores
    name_score = 0.0
    if ocr_name and card_name:
        # Use rapidfuzz or difflib for single comparison
        if HAVE_RAPIDFUZZ:
            name_score = rf_fuzz.WRatio(_normalize(ocr_name), _normalize(card_name))
        else:
            name_score = difflib.SequenceMatcher(None, _normalize(ocr_name), _normalize(card_name)).ratio() * 100
    
    oracle_score = _oracle_similarity(ocr_oracle, card_oracle)
    type_score = _type_similarity(ocr_type, card_type)
    set_score = _set_similarity(ocr_set, card_set)
    collector_score = _collector_similarity(ocr_collector, card_collector)
    
    # Calculate weighted total
    total = (
        name_score * weights.get("name", 0.6) +
        oracle_score * weights.get("oracle", 0.15) +
        type_score * weights.get("type", 0.05) +
        set_score * weights.get("set", 0.05) +
        collector_score * weights.get("collector", 0.15)
    )
    
    return {
        "card": card,
        "name_score": float(name_score),
        "oracle_score": float(oracle_score),
        "type_score": float(type_score),
        "set_score": float(set_score),
        "collector_score": float(collector_score),
        "total_score": float(total),
    }


# ------ public API ------

def identify_card_from_ocr(
    ocr_map: Dict[str, str],
    db_path: Optional[str] = None,
    cards_list: Optional[List[Dict[str, Any]]] = None,
    top_n: int = 8,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Identify the most probable card given OCR regions using lightweight string matching.
    
    Args:
        ocr_map: Dictionary with OCR extracted text regions (name, oracle, collector, set, type, etc.)
        db_path: Path to card database (JSON, NDJSON, or SQLite)
        cards_list: Direct list of card dictionaries (alternative to db_path)
        top_n: Number of candidates to consider in fuzzy matching
        weights: Dictionary of scoring weights (name, oracle, type, set, collector)
    
    Returns:
        {
            'best': {card dict or None},
            'score': combined_score (0..100),
            'candidates': [ {card, name_score, oracle_score, ..., total_score}, ... ],
            'debug': {...}
        }
    """
    if cards_list is None and not db_path:
        raise ValueError("Provide either db_path or cards_list")
    
    # Load cards
    cards = cards_list if cards_list is not None else load_local_db(db_path)
    
    # Default weights if not provided
    if weights is None:
        weights = {
            "name": 0.70,       # Name is most important - increased from 0.6
            "oracle": 0.10,     # Oracle text helps distinguish - decreased from 0.15
            "collector": 0.15,  # Collector number is unique per set
            "type": 0.03,       # Type line helps a bit - decreased from 0.05
            "set": 0.02,        # Set code helps a bit - decreased from 0.05
        }
    
    # Extract OCR data
    ocr_name = (ocr_map.get("name") or ocr_map.get("title") or "").strip()
    ocr_oracle = (ocr_map.get("oracle") or ocr_map.get("rules") or "").strip()
    ocr_collector = (ocr_map.get("collector") or ocr_map.get("collector_number") or "").strip()
    ocr_set = (ocr_map.get("set") or ocr_map.get("set_code") or "").strip()
    
    results = {
        'best': None,
        'score': 0.0,
        'candidates': [],
        'debug': {
            'ocr_name': ocr_name,
            'ocr_oracle': ocr_oracle,
            'ocr_collector': ocr_collector,
            'ocr_set': ocr_set,
            'num_cards_in_db': len(cards),
            'matching_strategy': 'lightweight',
        }
    }
    
    # Strategy 1: Exact name match (fastest and most reliable)
    exact_match = _exact_name_match(ocr_name, cards)
    if exact_match:
        scored = _score_candidate(ocr_map, exact_match, weights)
        results['best'] = exact_match
        results['score'] = 100.0  # Exact match gets perfect score
        results['candidates'] = [scored]
        results['debug']['match_type'] = 'exact_name'
        LOG.debug("Exact name match found: %s", exact_match.get("name"))
        return results
    
    # Strategy 2: Collector number + set match (very reliable)
    collector_match = _collector_set_match(ocr_collector, ocr_set, cards)
    if collector_match:
        scored = _score_candidate(ocr_map, collector_match, weights)
        results['best'] = collector_match
        results['score'] = scored['total_score']
        results['candidates'] = [scored]
        results['debug']['match_type'] = 'collector_set'
        LOG.debug("Collector/set match found: %s", collector_match.get("name"))
        return results
    
    # Strategy 3: Fuzzy name matching with multi-criteria scoring
    LOG.debug("Performing fuzzy name search for: %s", ocr_name)
    name_candidates = _fuzzy_name_matches(ocr_name, cards, top_n=top_n)
    
    if not name_candidates:
        LOG.warning("No fuzzy name matches found for: %s", ocr_name)
        return results
    
    LOG.debug("Found %d name candidates for '%s'", len(name_candidates), ocr_name)
    
    # Score all candidates
    scored_candidates = []
    for card, name_score in name_candidates:
        scored = _score_candidate(ocr_map, card, weights)
        scored_candidates.append(scored)
    
    # Sort by total score descending
    scored_candidates.sort(key=lambda x: x['total_score'], reverse=True)
    
    results['candidates'] = scored_candidates
    if scored_candidates:
        results['best'] = scored_candidates[0]['card']
        results['score'] = scored_candidates[0]['total_score']
        results['debug']['match_type'] = 'fuzzy_multi_criteria'
        
        # Log top 3 candidates for debugging inconsistencies
        LOG.info("Identification for '%s':", ocr_name)
        LOG.info("  Best match: %s (score: %.2f)", 
                results['best'].get("name"), results['score'])
        for i, candidate in enumerate(scored_candidates[:3], 1):
            LOG.info("  %d. %s - total: %.2f (name: %.2f, oracle: %.2f, collector: %.2f)",
                    i,
                    candidate['card'].get('name', 'Unknown'),
                    candidate['total_score'],
                    candidate['name_score'],
                    candidate['oracle_score'],
                    candidate['collector_score'])
        
        # Warning if top scores are very close (potential ambiguity)
        if len(scored_candidates) > 1:
            score_diff = scored_candidates[0]['total_score'] - scored_candidates[1]['total_score']
            if score_diff < 5.0:
                LOG.warning("  ⚠ Top 2 candidates have very close scores (diff: %.2f) - identification may be unreliable!", 
                          score_diff)
    
    return results


# CLI for testing
if __name__ == "__main__":
    import sys
    import pprint
    
    if len(sys.argv) < 3:
        print("Usage: card_id_lightweight.py <db.json|db.sqlite> <ocr_json>")
        print("Example: card_id_lightweight.py data/oracle-cards.json sample_ocr.json")
        sys.exit(1)
    
    db = sys.argv[1]
    ocr_file = sys.argv[2]
    
    with open(ocr_file, "r", encoding="utf8") as fh:
        ocr_map = json.load(fh)
    
    result = identify_card_from_ocr(ocr_map, db_path=db)
    pprint.pprint(result)
