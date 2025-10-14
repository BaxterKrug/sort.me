# services/assign.py
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------- Data types ----------
@dataclass
class Card:
    game: str
    name: str
    set_code: Optional[str] = None
    collector_number: Optional[str] = None
    confidence: float = 1.0

@dataclass
class Cell:
    id: str
    capacity: int
    tags: List[str]

@dataclass
class SystemState:
    counts_by_cell: Dict[str, int]   # live counts per cell

@dataclass
class Config:
    # thresholds
    low_conf_thresh: float
    near_full_thresh: float

    # cells & feeder rule
    cells: Dict[str, Cell]
    feeder_re: Optional[re.Pattern]

    # mapping & overflow
    letter_to_cell: Dict[str, str]   # 'A'..'Z' -> cell_id
    overflow_cells: List[str]

# ---------- Loader ----------
def load_config(yaml_dict: dict) -> Config:
    # cells
    cell_map: Dict[str, Cell] = {}
    for c in yaml_dict.get("cells", []):
        cell_map[c["id"]] = Cell(
            id=c["id"],
            capacity=int(c.get("capacity", 999999)),
            tags=[str(t) for t in c.get("tags", [])]
        )

    # feeder regex
    feeder_pat = yaml_dict.get("feeder", {}).get("reserve_pattern")
    feeder_re = re.compile(feeder_pat) if feeder_pat else None

    # mapping (manual only)
    letter_to_cell = {k.upper(): str(v) for k, v in yaml_dict["alpha_exact"]["letter_to_cell"].items()}
    # quick validation: mapping targets must exist and not be feeders
    for letter, cid in letter_to_cell.items():
        if cid not in cell_map:
            raise ValueError(f"alpha_exact: cell '{cid}' for letter '{letter}' not defined in cells:")
        if feeder_re and feeder_re.search(cid):
            raise ValueError(f"alpha_exact: letter '{letter}' mapped to feeder cell '{cid}', which is forbidden")

    # overflow cells
    overflow_cells = [str(x) for x in yaml_dict.get("overflow", {}).get("cells", [])]
    if not overflow_cells:
        raise ValueError("overflow.cells must include at least one cell (e.g., ERR1)")
    for oc in overflow_cells:
        if oc not in cell_map:
            raise ValueError(f"overflow cell '{oc}' not defined in cells:")

    return Config(
        low_conf_thresh=float(yaml_dict.get("sorting", {}).get("low_confidence_threshold", 0.80)),
        near_full_thresh=float(yaml_dict.get("sorting", {}).get("near_full_threshold", 0.90)),
        cells=cell_map,
        feeder_re=feeder_re,
        letter_to_cell=letter_to_cell,
        overflow_cells=overflow_cells,
    )

# ---------- Helpers ----------
def _has_capacity(cell: Cell, state: SystemState) -> bool:
    return state.counts_by_cell.get(cell.id, 0) < cell.capacity

def _is_feeder(cell_id: str, feeder_re: Optional[re.Pattern]) -> bool:
    return bool(feeder_re and feeder_re.search(cell_id))

def _overflow_target(cfg: Config, state: SystemState) -> str:
    # pick first overflow cell with capacity; otherwise the first one anyway
    for cid in cfg.overflow_cells:
        c = cfg.cells[cid]
        if _has_capacity(c, state):
            return cid
    return cfg.overflow_cells[0]

# ---------- Core assignment ----------
def assign_card(card: Card, cfg: Config, state: SystemState) -> Tuple[str, str]:
    """
    Manual letter-based assignment:
      - If confidence < threshold -> overflow (ERR1)
      - Determine first A–Z; map via cfg.letter_to_cell
      - If target full -> overflow
      - Never place into feeder cells (assert)
    Returns: (cell_id, reason)
    """
    # 1) Confidence gate
    if card.confidence < cfg.low_conf_thresh:
        target = _overflow_target(cfg, state)
        return target, "divert:low_confidence"

    # 2) Letter mapping (non A–Z defaults to 'A')
    first = (card.name.strip()[:1] or "#").upper()
    if first < "A" or first > "Z":
        first = "A"

    target_id = cfg.letter_to_cell[first]
    # safety: never feeder
    assert not _is_feeder(target_id, cfg.feeder_re), f"Mapping points to feeder cell: {target_id}"

    # 3) Capacity check
    target_cell = cfg.cells[target_id]
    if _has_capacity(target_cell, state):
        return target_id, f"alpha_exact:{first}"

    # 4) Overflow
    overflow_id = _overflow_target(cfg, state)
    return overflow_id, f"overflow:{first}"
