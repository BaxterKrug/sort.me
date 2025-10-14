"""
Card identification helper.

Given OCR region->text (e.g. {'name': "...", 'oracle': "...", 'collector': "12/264", ...})
attempt to find the best matching card from a local card database.

Database loader supports:
 - JSON file containing a list of card objects (common keys: 'name','oracle_text','collector_number','set','id')
 - NDJSON (one JSON object per line)
 - SQLite DB with a 'cards' table (columns: name, oracle_text, collector_number, set_code, id)

Matching strategy:
 - exact normalized name -> immediate match
 - collector number + set -> strong match
 - fuzzy name match (rapidfuzz if available, otherwise difflib)
 - refine candidates by checking oracle/type tokens overlap
 - return best candidate + debug scoring info
"""

from typing import Dict, Any, List, Optional, Tuple
import json
import os
import sqlite3
import unicodedata
import re

# try to use rapidfuzz for better fuzzy matching, otherwise fallback
try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except Exception:
    HAVE_RAPIDFUZZ = False
    import difflib

# ------ helpers ------

def _normalize(s: Optional[str]) -> str:
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

# ------ scoring / matching ------

def _name_candidates_from_db(name: str, cards: List[Dict[str,Any]], top_n: int = 10) -> List[Tuple[Dict[str,Any], float]]:
    """
    Return up to top_n candidate cards with a name similarity score (0..100).
    """
    if not name:
        return []
    norm_name = _normalize(name)
    # build name list and mapping
    name_map = {}
    names = []
    for c in cards:
        n = _normalize(c.get("name") or c.get("title") or "")
        names.append(n)
        name_map[n] = name_map.get(n, []) + [c]
    if HAVE_RAPIDFUZZ:
        # rapidfuzz can return (match, score, index)
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

def _oracle_overlap_score(ocr_oracle: str, card_oracle: str) -> float:
    """
    Compute a simple token overlap score (0..1) between OCR oracle text and card oracle text.
    """
    if not ocr_oracle or not card_oracle:
        return 0.0
    toks_a = set([t for t in re.split(r"\W+", _normalize(ocr_oracle)) if t])
    toks_b = set([t for t in re.split(r"\W+", _normalize(card_oracle)) if t])
    if not toks_a or not toks_b:
        return 0.0
    inter = toks_a.intersection(toks_b)
    # Jaccard-like but weighted towards OCR tokens (we want most OCR tokens found in the card)
    score = len(inter) / max(1, len(toks_a))
    return float(score)

# ------ public API ------

def identify_card_from_ocr(ocr_map: Dict[str,str],
                           db_path: Optional[str] = None,
                           cards_list: Optional[List[Dict[str,Any]]] = None,
                           top_n: int = 8,
                           name_weight: float = 0.75,
                           oracle_weight: float = 0.20,
                           collector_weight: float = 0.05
                           ) -> Dict[str,Any]:
    """
    Identify the most probable card given OCR regions.

    Returns:
     {
       'best': {card dict or None},
       'score': combined_score (0..100),
       'candidates': [ {card, name_score, oracle_score, collector_score, total_score}, ... ],
       'debug': {...}
     }

    Provide either db_path to load local DB or cards_list directly.
    """
    if not cards_list and not db_path:
        raise ValueError("Provide db_path or cards_list")
    cards = cards_list if cards_list is not None else load_local_db(db_path)
    # normalize OCRed regions
    o_name = (ocr_map.get("name") or ocr_map.get("title") or "").strip()
    o_oracle = (ocr_map.get("oracle") or ocr_map.get("rules") or "").strip()
    o_collector = (ocr_map.get("collector") or "").strip()

    norm_o_name = _normalize(o_name)

    results = {
        'best': None,
        'score': 0.0,
        'candidates': [],
        'debug': {
            'ocr_name': o_name,
            'ocr_oracle': o_oracle,
            'ocr_collector': o_collector,
            'num_cards_in_db': len(cards)
        }
    }

    # 1) try exact normalized name match
    if norm_o_name:
        for c in cards:
            if _normalize(c.get("name") or c.get("title") or "") == norm_o_name:
                # immediate perfect-ish match
                results['best'] = c
                results['score'] = 100.0
                results['candidates'] = [{
                    'card': c, 'name_score': 100.0, 'oracle_score': 1.0, 'collector_score': 1.0, 'total_score': 100.0
                }]
                return results

    # 2) if collector present, try collector+set exact match (collector often unique)
    if o_collector:
        oc_norm = o_collector.strip()
        for c in cards:
            cc = str(c.get("collector_number") or c.get("collector") or "").strip()
            setc = str(c.get("set") or c.get("set_code") or "").strip()
            if cc and cc == oc_norm:
                # prefer if set matches or name similar
                name_score = 100.0 if norm_o_name and _normalize(c.get("name","")) == norm_o_name else 85.0
                total = name_score * name_weight + 100.0 * collector_weight
                results['best'] = c
                results['score'] = total
                results['candidates'] = [{
                    'card': c, 'name_score': name_score, 'oracle_score': 0.0, 'collector_score': 100.0, 'total_score': total
                }]
                return results

    # 3) fuzzy name candidates
    name_cands = _name_candidates_from_db(o_name, cards, top_n=top_n)
    scored = []
    for cand, name_score in name_cands:
        oracle_score = _oracle_overlap_score(o_oracle, cand.get("oracle_text") or cand.get("oracle") or "")
        collector_score = 100.0 if o_collector and str(cand.get("collector_number") or cand.get("collector") or "").strip() == o_collector.strip() else 0.0
        # combine into 0..100
        total = (name_score * name_weight) + (oracle_score * 100.0 * oracle_weight) + (collector_score * collector_weight)
        scored.append({
            'card': cand,
            'name_score': float(name_score),
            'oracle_score': float(oracle_score),
            'collector_score': float(collector_score),
            'total_score': float(total)
        })
    # sort by total_score desc
    scored.sort(key=lambda x: x['total_score'], reverse=True)
    results['candidates'] = scored
    if scored:
        results['best'] = scored[0]['card']
        results['score'] = scored[0]['total_score']
    return results

# small CLI for quick manual testing
if __name__ == "__main__":
    import sys, pprint
    if len(sys.argv) < 3:
        print("Usage: card_id.py <db.json|db.sqlite> <image_ocr_json>")
        print("Example: card_id.py /path/to/cards.json sample_ocr.json")
        sys.exit(1)
    db = sys.argv[1]
    ocr_file = sys.argv[2]
    with open(ocr_file, "r", encoding="utf8") as fh:
        ocr_map = json.load(fh)
    out = identify_card_from_ocr(ocr_map, db_path=db)
    pprint.pprint(out)