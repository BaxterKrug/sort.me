# services/assign.py
from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from datetime import datetime
import json

LOG = logging.getLogger("sort.assign")

DEFAULT_SORT_OPERATION = "default"
DEFAULT_SORT_MODE = "alpha_exact"

# ---------- Data types ----------
@dataclass
class Card:
    game: str
    name: str
    set_code: Optional[str] = None
    collector_number: Optional[str] = None
    scryfall_id: Optional[str] = None
    confidence: float = 1.0
    printed_name: Optional[str] = None
    flavor_name: Optional[str] = None
    set_name: Optional[str] = None
    released_at: Optional[str] = None
    released_year: Optional[str] = None
    price_usd: Optional[str] = None

    def display_name(self) -> str:
        """Prefer printed/flavor names for display/sorting when available."""
        for candidate in (self.printed_name, self.flavor_name, self.name):
            if candidate and str(candidate).strip():
                return str(candidate).strip()
        return self.name

@dataclass
class Cell:
    id: str
    capacity: int
    tags: List[str]

@dataclass
class SystemState:
    counts_by_cell: Dict[str, int]   # live counts per cell
    feeder_counts: Dict[str, int] = field(default_factory=dict)
    active_sort_operation: Optional[str] = None
    active_sort_mode: Optional[str] = None

    def feeder_remaining(self, cell_id: str) -> Optional[int]:
        if cell_id not in self.feeder_counts:
            return None
        return max(0, int(self.feeder_counts[cell_id]))

    def decrement_feeder(self, cell_id: str) -> Optional[int]:
        if cell_id not in self.feeder_counts:
            return None
        self.feeder_counts[cell_id] = max(0, int(self.feeder_counts[cell_id]) - 1)
        return self.feeder_counts[cell_id]

@dataclass
class Config:
    # thresholds
    low_conf_thresh: float
    near_full_thresh: float

    # cells & feeder rule
    cells: Dict[str, Cell]
    feeder_re: Optional[re.Pattern]
    feeder_sequence: List[str]  # Explicit sequence of feeder cell IDs

    # mapping & overflow
    letter_to_cell: Dict[str, str]   # 'A'..'Z' -> cell_id (alpha mode shortcut)
    overflow_cells: List[str]
    default_sort_mode: str = "alpha_exact"
    sort_operations: Dict[str, Dict[str, str]] = field(default_factory=dict)
    default_sort_operation: Optional[str] = None
    sort_modes: Dict[str, "SortMode"] = field(default_factory=dict)


@dataclass
class SortMode:
    id: str
    type: str  # 'alpha', 'year', 'set', 'price'
    label: str
    mapping: Dict[str, str]
    default_cell: Optional[str] = None
    price_thresholds: Optional[List[Dict[str, Any]]] = None  # For price mode


_DETAIL_PROVIDER: Optional[Callable[[Optional[str]], Dict[str, Any]]] = None


def configure_detail_provider(provider: Optional[Callable[[Optional[str]], Dict[str, Any]]]) -> None:
    global _DETAIL_PROVIDER
    _DETAIL_PROVIDER = provider

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

    # feeder configuration
    feeder_cfg = yaml_dict.get("feeder", {})
    feeder_pat = feeder_cfg.get("reserve_pattern")
    feeder_re = re.compile(feeder_pat) if feeder_pat else None
    
    # Build feeder sequence: explicit list > tagged cells > regex pattern
    feeder_sequence: List[str] = []
    explicit_seq = feeder_cfg.get("sequence")
    if explicit_seq and isinstance(explicit_seq, list):
        # Use explicit sequence from config
        feeder_sequence = [str(cid).upper() for cid in explicit_seq if str(cid).upper() in cell_map]
    
    if not feeder_sequence:
        # Fall back to cells tagged as 'feeder'
        feeder_sequence = [cid for cid, cell in cell_map.items() if 'feeder' in cell.tags]
    
    if not feeder_sequence and feeder_re:
        # Fall back to regex pattern
        feeder_sequence = [cid for cid in cell_map.keys() if feeder_re.search(cid)]

    # mapping (manual only)
    base_alpha_map = {k.upper(): str(v).upper() for k, v in yaml_dict["alpha_exact"]["letter_to_cell"].items()}
    _validate_cell_mapping(base_alpha_map, cell_map, feeder_re, "alpha_exact", "letter_to_cell")

    # overflow cells
    overflow_cells = [str(x) for x in yaml_dict.get("overflow", {}).get("cells", [])]
    if not overflow_cells:
        raise ValueError("overflow.cells must include at least one cell (e.g., ERR1)")
    for oc in overflow_cells:
        if oc not in cell_map:
            raise ValueError(f"overflow cell '{oc}' not defined in cells:")

    sorting_cfg = yaml_dict.get("sorting", {}) if isinstance(yaml_dict, dict) else {}
    default_sort_mode = str(sorting_cfg.get("default_mode", DEFAULT_SORT_MODE)).strip().lower() or DEFAULT_SORT_MODE

    sort_operations: Dict[str, Dict[str, str]] = {}
    operations_csv = sorting_cfg.get("operations_csv") if isinstance(sorting_cfg, dict) else None
    if operations_csv:
        try:
            sort_operations = _load_sort_operations_csv(operations_csv)
        except FileNotFoundError:
            LOG.warning("sorting.operations_csv %s not found; ignoring", operations_csv)
        except Exception as exc:  # pragma: no cover - config errors logged
            LOG.warning("Failed to load sorting operations from %s: %s", operations_csv, exc)

    default_sort_operation = sorting_cfg.get("default_sort_operation") if isinstance(sorting_cfg, dict) else None
    if isinstance(default_sort_operation, str):
        default_sort_operation = default_sort_operation.strip().lower() or None
    if not default_sort_operation and sort_operations:
        if DEFAULT_SORT_OPERATION in sort_operations:
            default_sort_operation = DEFAULT_SORT_OPERATION
        else:
            default_sort_operation = next(iter(sort_operations))

    sort_modes = _build_sort_modes(
        base_alpha_map,
        cell_map,
        feeder_re,
        sorting_cfg.get("modes") if isinstance(sorting_cfg, dict) else {},
    )

    if default_sort_mode not in sort_modes:
        LOG.warning("Default sort mode '%s' not configured; falling back to '%s'", default_sort_mode, DEFAULT_SORT_MODE)
        default_sort_mode = DEFAULT_SORT_MODE

    letter_to_cell = sort_modes[DEFAULT_SORT_MODE].mapping

    return Config(
        low_conf_thresh=float(sorting_cfg.get("low_confidence_threshold", 0.80)),
        near_full_thresh=float(sorting_cfg.get("near_full_threshold", 0.90)),
        cells=cell_map,
        feeder_re=feeder_re,
        feeder_sequence=feeder_sequence,
        letter_to_cell=letter_to_cell,
        overflow_cells=overflow_cells,
        default_sort_mode=default_sort_mode,
        sort_operations=sort_operations,
        default_sort_operation=default_sort_operation,
        sort_modes=sort_modes,
    )


def _normalize_cell_id(cell_id: Optional[str]) -> Optional[str]:
    if cell_id is None:
        return None
    cid = str(cell_id).strip().upper()
    return cid or None


def _ensure_cell(
    cell_id: Optional[str],
    cell_map: Dict[str, Cell],
    feeder_re: Optional[re.Pattern],
    section: str,
    key: str,
) -> str:
    cid = _normalize_cell_id(cell_id)
    if not cid:
        raise ValueError(f"{section}: missing cell id for '{key}'")
    if cid not in cell_map:
        raise ValueError(f"{section}: cell '{cid}' referenced by '{key}' not defined in cells")
    if feeder_re and feeder_re.search(cid):
        raise ValueError(f"{section}: cell '{cid}' referenced by '{key}' is a feeder and cannot be targeted")
    return cid


def _validate_cell_mapping(
    mapping: Dict[str, str],
    cell_map: Dict[str, Cell],
    feeder_re: Optional[re.Pattern],
    section: str,
    field: str,
) -> None:
    for key, value in list(mapping.items()):
        mapping[key] = _ensure_cell(value, cell_map, feeder_re, section, f"{field}[{key}]")


def _prepare_alpha_mapping(
    raw_map: Dict[str, str],
    cell_map: Dict[str, Cell],
    feeder_re: Optional[re.Pattern],
    section: str,
) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for letter, cell_id in raw_map.items():
        key = str(letter).strip().upper()
        if len(key) != 1 or not key.isalpha():
            raise ValueError(f"{section}: invalid letter key '{letter}'")
        mapping[key] = _ensure_cell(cell_id, cell_map, feeder_re, section, letter)
    return mapping


def _normalize_year_key(raw: Any) -> str:
    s = str(raw).strip()
    if not s:
        raise ValueError("empty year key")
    if s == "*" or s.lower() == "default":
        return "*"
    if s.endswith("s") and len(s) == 5 and s[:4].isdigit():
        return f"{s[:4]}s"
    if "-" in s:
        start, end = s.split("-", 1)
        if len(start) == 4 and len(end) == 4 and start.isdigit() and end.isdigit():
            if int(start) > int(end):
                raise ValueError(f"invalid year range '{s}'")
            return f"{start}-{end}"
    if s.startswith("<=") or s.startswith(">="):
        num = s[2:]
        if len(num) == 4 and num.isdigit():
            return f"{s[:2]}{num}"
    if s.isdigit() and len(s) == 4:
        return s
    raise ValueError(f"Unrecognized year key '{raw}'")


def _prepare_year_mapping(
    raw_map: Dict[str, str],
    cell_map: Dict[str, Cell],
    feeder_re: Optional[re.Pattern],
    section: str,
) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for raw_key, cell_id in raw_map.items():
        key = _normalize_year_key(raw_key)
        mapping[key] = _ensure_cell(cell_id, cell_map, feeder_re, section, raw_key)
    return mapping


def _normalize_set_key(raw: Any) -> str:
    s = str(raw).strip()
    if not s:
        raise ValueError("empty set key")
    if s == "*" or s.lower() == "default":
        return "*"
    return s.lower()


def _prepare_set_mapping(
    raw_map: Dict[str, str],
    cell_map: Dict[str, Cell],
    feeder_re: Optional[re.Pattern],
    section: str,
) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for raw_key, cell_id in raw_map.items():
        key = _normalize_set_key(raw_key)
        mapping[key] = _ensure_cell(cell_id, cell_map, feeder_re, section, raw_key)
    return mapping


def _build_sort_modes(
    base_alpha_map: Dict[str, str],
    cell_map: Dict[str, Cell],
    feeder_re: Optional[re.Pattern],
    modes_cfg: Any,
) -> Dict[str, SortMode]:
    sort_modes: Dict[str, SortMode] = {}

    alpha_cfg = modes_cfg.get(DEFAULT_SORT_MODE) if isinstance(modes_cfg, dict) else None
    alpha_map = dict(base_alpha_map)
    alpha_label = "Alphabetical (A–Z)"
    alpha_type = "alpha"
    alpha_default_cell: Optional[str] = None

    if isinstance(alpha_cfg, dict):
        raw_alpha_map = alpha_cfg.get("letter_to_cell") or alpha_cfg.get("mapping")
        if isinstance(raw_alpha_map, dict) and raw_alpha_map:
            alpha_map = _prepare_alpha_mapping(raw_alpha_map, cell_map, feeder_re, "sorting.modes.alpha_exact")
        alpha_label = str(alpha_cfg.get("label") or alpha_label)
        alpha_type = str(alpha_cfg.get("type") or "alpha").strip().lower() or "alpha"
        fallback = alpha_cfg.get("fallback") or alpha_cfg.get("default_cell")
        if fallback:
            alpha_default_cell = _ensure_cell(fallback, cell_map, feeder_re, "sorting.modes.alpha_exact", "fallback")

    sort_modes[DEFAULT_SORT_MODE] = SortMode(
        id=DEFAULT_SORT_MODE,
        type=alpha_type,
        label=alpha_label,
        mapping=dict(alpha_map),
        default_cell=alpha_default_cell,
    )

    if not isinstance(modes_cfg, dict):
        return sort_modes

    for mode_id, mode_cfg in modes_cfg.items():
        if mode_id == DEFAULT_SORT_MODE:
            continue
        if not isinstance(mode_cfg, dict):
            continue
        mode_type = str(mode_cfg.get("type") or mode_id).strip().lower()
        label = str(mode_cfg.get("label") or mode_id.replace("_", " ").title())
        fallback = mode_cfg.get("fallback") or mode_cfg.get("default_cell")
        default_cell = None
        if fallback:
            default_cell = _ensure_cell(fallback, cell_map, feeder_re, f"sorting.modes.{mode_id}", "fallback")

        raw_map = mode_cfg.get("mapping")
        if mode_type == "alpha" and not raw_map:
            raw_map = mode_cfg.get("letter_to_cell")
        elif mode_type == "year" and not raw_map:
            raw_map = mode_cfg.get("year_to_cell")
        elif mode_type == "set" and not raw_map:
            raw_map = mode_cfg.get("set_to_cell")
        elif mode_type == "price":
            # Price mode uses thresholds instead of mapping
            raw_map = None

        price_thresholds = None
        if mode_type == "price":
            price_thresholds = mode_cfg.get("thresholds")
            if not isinstance(price_thresholds, list) or not price_thresholds:
                LOG.warning("Sort mode '%s' of type 'price' missing thresholds; skipping", mode_id)
                continue
            # Validate threshold entries
            for i, threshold in enumerate(price_thresholds):
                if not isinstance(threshold, dict):
                    LOG.warning("Sort mode '%s' threshold %d is not a dict; skipping mode", mode_id, i)
                    break
                cell_id = threshold.get("cell")
                if not cell_id:
                    LOG.warning("Sort mode '%s' threshold %d missing 'cell'; skipping mode", mode_id, i)
                    break
                # Ensure cell exists
                _ensure_cell(cell_id, cell_map, feeder_re, f"sorting.modes.{mode_id}", f"threshold[{i}].cell")
            else:
                # All thresholds validated successfully
                mapping = {}  # Price mode doesn't use traditional mapping
        elif not isinstance(raw_map, dict) or not raw_map:
            LOG.warning("Sort mode '%s' missing mapping; skipping", mode_id)
            continue

        if mode_type == "alpha" and raw_map:
            mapping = _prepare_alpha_mapping(raw_map, cell_map, feeder_re, f"sorting.modes.{mode_id}")
        elif mode_type == "year" and raw_map:
            mapping = _prepare_year_mapping(raw_map, cell_map, feeder_re, f"sorting.modes.{mode_id}")
        elif mode_type == "set" and raw_map:
            mapping = _prepare_set_mapping(raw_map, cell_map, feeder_re, f"sorting.modes.{mode_id}")
        elif mode_type == "price":
            # Already validated above, mapping is empty dict
            pass
        else:
            LOG.warning("Sort mode '%s' has unknown type '%s'; skipping", mode_id, mode_type)
            continue

        sort_modes[mode_id] = SortMode(
            id=mode_id,
            type=mode_type,
            label=label,
            mapping=mapping,
            default_cell=default_cell,
            price_thresholds=price_thresholds,
        )

    return sort_modes

# ---------- Helpers ----------
def _load_sort_operations_csv(csv_path: str) -> Dict[str, Dict[str, str]]:
    """
    Load sort operations from a CSV file.

    Expected columns:
      - scryfall_id (required)
      - target_cell (required)
      - operation (optional, defaults to 'default')
    Additional columns are ignored.
    """

    path = Path(csv_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise FileNotFoundError(str(path))

    operations: Dict[str, Dict[str, str]] = {}
    with path.open("r", encoding="utf8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return operations
        for idx, row in enumerate(reader, start=2):
            if not isinstance(row, dict):
                continue
            raw_id = (row.get("scryfall_id") or row.get("id") or row.get("card_id") or "").strip()
            if not raw_id:
                continue
            scryfall_id = raw_id.lower()
            target_cell = (row.get("target_cell") or row.get("cell") or row.get("destination") or "").strip()
            if not target_cell:
                continue
            operation = (row.get("operation") or row.get("sort") or row.get("mode") or "").strip() or DEFAULT_SORT_OPERATION
            operation = operation.lower()
            target_cell = target_cell.upper()
            if operation not in operations:
                operations[operation] = {}
            if scryfall_id in operations[operation]:
                LOG.warning(
                    "Duplicate sort operation for %s in %s (line %d); overwriting previous target %s -> %s",
                    raw_id,
                    path,
                    idx,
                    operations[operation][scryfall_id],
                    target_cell,
                )
            operations[operation][scryfall_id] = target_cell
    return operations


def _append_scan_csv(card: Card, cell_id: str, reason: str) -> None:
    """Append a row to data/scanned_cards.csv recording the scan.

    Fields: timestamp_iso, scryfall_id, name, assigned_cell, reason, confidence
    If card.scryfall_id is not set, attempt to read it from data/snapshots/last_snapshot.json.
    """
    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path = data_dir / "scanned_cards.csv"

    scry_id = card.scryfall_id
    if not scry_id:
        try:
            lastp = Path("data") / "snapshots" / "last_snapshot.json"
            if lastp.exists():
                txt = lastp.read_text(encoding="utf8")
                try:
                    scry = json.loads(txt)
                    if isinstance(scry, str):
                        scry_id = scry
                except Exception:
                    # tolerate non-json content
                    scry_id = txt.strip() or None
        except Exception:
            scry_id = None

    row = {
        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "scryfall_id": scry_id or "",
        "name": (card.name or "") if isinstance(card.name, str) else str(card.name or ""),
        "assigned_cell": cell_id,
        "reason": reason,
        "confidence": f"{float(card.confidence):.3f}",
    }

    write_header = not csv_path.exists()
    with csv_path.open("a", encoding="utf8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["timestamp", "scryfall_id", "name", "assigned_cell", "reason", "confidence"])
        if write_header:
            writer.writeheader()
        writer.writerow(row)


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


def _populate_card_details(card: Card) -> None:
    if not _DETAIL_PROVIDER or not card.scryfall_id:
        return
    try:
        details = _DETAIL_PROVIDER(card.scryfall_id)
    except Exception:
        return
    if not isinstance(details, dict):
        return
    card.name = details.get("name") or card.name
    card.printed_name = details.get("printed_name") or card.printed_name
    card.flavor_name = details.get("flavor_name") or card.flavor_name
    card.set_code = details.get("set_code") or card.set_code
    card.set_name = details.get("set_name") or card.set_name
    card.released_at = details.get("released_at") or card.released_at
    year = details.get("released_year") or card.released_year
    if not year and card.released_at:
        year = str(card.released_at)[:4]
    if year:
        card.released_year = str(year)
    price = details.get("price_usd") or card.price_usd
    if not price:
        prices_map = details.get("prices") if isinstance(details.get("prices"), dict) else {}
        price = prices_map.get("usd") if prices_map else None
    if price:
        card.price_usd = str(price)


def _extract_release_year(card: Card) -> Optional[int]:
    if card.released_year:
        year_str = str(card.released_year).strip()
        if len(year_str) == 4 and year_str.isdigit():
            return int(year_str)
    if card.released_at:
        year_str = str(card.released_at).strip()[:4]
        if len(year_str) == 4 and year_str.isdigit():
            card.released_year = year_str
            return int(year_str)
    return None


def _lookup_year_cell(mapping: Dict[str, str], year: int) -> Optional[Tuple[str, str]]:
    year_str = f"{year:04d}"
    if year_str in mapping:
        return year_str, mapping[year_str]
    decade_key = f"{(year // 10) * 10}s"
    if decade_key in mapping:
        return decade_key, mapping[decade_key]
    for key, cell_id in mapping.items():
        if "-" in key:
            start, end = key.split("-", 1)
            if start.isdigit() and end.isdigit() and int(start) <= year <= int(end):
                return key, cell_id
        if key.startswith("<=") and key[2:].isdigit() and year <= int(key[2:]):
            return key, cell_id
        if key.startswith(">=") and key[2:].isdigit() and year >= int(key[2:]):
            return key, cell_id
    if "*" in mapping:
        return "*", mapping["*"]
    return None


def _lookup_set_cell(mapping: Dict[str, str], set_code: Optional[str], set_name: Optional[str]) -> Optional[Tuple[str, str]]:
    candidates: List[str] = []
    if set_code:
        candidates.append(set_code.lower())
    if set_name:
        candidates.append(set_name.strip().lower())
    candidates.append("*")
    for key in candidates:
        if key in mapping:
            return key, mapping[key]
    return None


def _apply_target_cell(
    cell_id: str,
    mode: SortMode,
    key: str,
    reason_name: str,
    cfg: Config,
    state: SystemState,
) -> Tuple[str, str]:
    primary_cell = cfg.cells.get(cell_id)
    if primary_cell and _has_capacity(primary_cell, state):
        return cell_id, f"{mode.id}:{key}:{reason_name}"

    if mode.default_cell:
        fallback_id = mode.default_cell
        fallback_cell = cfg.cells.get(fallback_id)
        if fallback_cell and _has_capacity(fallback_cell, state):
            return fallback_id, f"{mode.id}:{key}:fallback:{reason_name}"

    overflow_id = _overflow_target(cfg, state)
    return overflow_id, f"overflow:{mode.id}:{key}:{reason_name}"


def _assign_alpha_mode(
    card: Card,
    mode: SortMode,
    reason_name: str,
    cfg: Config,
    state: SystemState,
) -> Tuple[str, str]:
    display_name = (card.display_name() or "").strip()
    letter_basis = display_name or reason_name
    first = (letter_basis[:1] or "#").upper()
    if first < "A" or first > "Z":
        first = "A"

    target_id = mode.mapping.get(first)
    if not target_id and mode.default_cell:
        return _apply_target_cell(mode.default_cell, mode, "default", reason_name, cfg, state)
    if not target_id:
        fallback_cell = cfg.letter_to_cell.get(first) or cfg.letter_to_cell.get("A")
        if not fallback_cell:
            overflow_id = _overflow_target(cfg, state)
            return overflow_id, f"overflow:{mode.id}:{first}:{reason_name}"
        return _apply_target_cell(fallback_cell, mode, first, reason_name, cfg, state)
    return _apply_target_cell(target_id, mode, first, reason_name, cfg, state)


def _assign_year_mode(
    card: Card,
    mode: SortMode,
    reason_name: str,
    cfg: Config,
    state: SystemState,
) -> Optional[Tuple[str, str]]:
    year = _extract_release_year(card)
    if year is None:
        _populate_card_details(card)
        year = _extract_release_year(card)
    if year is None:
        if "*" in mode.mapping:
            return _apply_target_cell(mode.mapping["*"], mode, "*", reason_name, cfg, state)
        if mode.default_cell:
            return _apply_target_cell(mode.default_cell, mode, "default", reason_name, cfg, state)
        return None
    year_match = _lookup_year_cell(mode.mapping, year)
    if not year_match:
        if mode.default_cell:
            return _apply_target_cell(mode.default_cell, mode, "default", reason_name, cfg, state)
        return None
    key, cell_id = year_match
    return _apply_target_cell(cell_id, mode, key, reason_name, cfg, state)


def _assign_set_mode(
    card: Card,
    mode: SortMode,
    reason_name: str,
    cfg: Config,
    state: SystemState,
) -> Optional[Tuple[str, str]]:
    set_code = (card.set_code or "").strip().lower()
    set_name = (card.set_name or "").strip().lower()
    if not set_code and not set_name:
        _populate_card_details(card)
        set_code = (card.set_code or "").strip().lower()
        set_name = (card.set_name or "").strip().lower()
    match = _lookup_set_cell(mode.mapping, set_code, set_name)
    if not match:
        if mode.default_cell:
            return _apply_target_cell(mode.default_cell, mode, "default", reason_name, cfg, state)
        return None
    key, cell_id = match
    display_key = key.upper() if key not in {"*", "default"} and key else key
    return _apply_target_cell(cell_id, mode, display_key or key, reason_name, cfg, state)


def _assign_price_mode(
    card: Card,
    mode: SortMode,
    reason_name: str,
    cfg: Config,
    state: SystemState,
) -> Optional[Tuple[str, str]]:
    """Assign card based on price thresholds.
    
    Thresholds define price ranges and their target cells.
    Cards with no price or N/A prices go to the lowest threshold cell (default_cell or first threshold).
    """
    # Get price from card
    price_str = (card.price_usd or "").strip().lower()
    LOG.debug("Price mode: Initial price_str for %s: %r", card.name, price_str)
    
    if not price_str or price_str in ("", "null", "none", "n/a", "na"):
        # No price available, try to populate
        LOG.debug("Price mode: No initial price, calling _populate_card_details for %s (scryfall_id=%s)", card.name, card.scryfall_id)
        _populate_card_details(card)
        price_str = (card.price_usd or "").strip().lower()
        LOG.debug("Price mode: After populate, price_str for %s: %r", card.name, price_str)
    
    # Parse price
    price_value: Optional[float] = None
    if price_str and price_str not in ("", "null", "none", "n/a", "na"):
        try:
            price_value = float(price_str)
            LOG.debug("Price mode: Parsed price for %s: $%.2f", card.name, price_value)
        except (ValueError, TypeError) as e:
            LOG.debug("Price mode: Failed to parse price %r for %s: %s", price_str, card.name, e)
    
    # If no valid price, use default cell or first threshold
    if price_value is None:
        LOG.info("Price mode: No valid price for %s, using default/first threshold cell", card.name)
        if mode.default_cell:
            return _apply_target_cell(mode.default_cell, mode, "no_price", reason_name, cfg, state)
        # Fall back to first threshold's cell
        if mode.price_thresholds and len(mode.price_thresholds) > 0:
            first_cell = mode.price_thresholds[0].get("cell")
            if first_cell:
                return _apply_target_cell(first_cell, mode, "no_price", reason_name, cfg, state)
        return None
    
    # Find matching threshold
    if not mode.price_thresholds:
        LOG.warning("Price mode: No thresholds configured!")
        return None
    
    LOG.debug("Price mode: Checking %d thresholds for %s ($%.2f)", len(mode.price_thresholds), card.name, price_value)
    
    for i, threshold in enumerate(mode.price_thresholds):
        min_price = threshold.get("min")
        max_price = threshold.get("max")
        cell_id = threshold.get("cell")
        
        LOG.debug("Price mode: Threshold %d: min=%s, max=%s, cell=%s", i, min_price, max_price, cell_id)
        
        if not cell_id:
            continue
        
        # Check if price falls within this threshold
        matches = True
        if min_price is not None:
            try:
                if price_value < float(min_price):
                    matches = False
                    LOG.debug("Price mode: Price $%.2f < min $%.2f, no match", price_value, float(min_price))
            except (ValueError, TypeError):
                matches = False
        
        if max_price is not None and matches:
            try:
                if price_value > float(max_price):
                    matches = False
                    LOG.debug("Price mode: Price $%.2f > max $%.2f, no match", price_value, float(max_price))
            except (ValueError, TypeError):
                matches = False
        
        if matches:
            price_label = f"${price_value:.2f}"
            LOG.info("Price mode: %s ($%.2f) matches threshold %d -> cell %s", card.name, price_value, i, cell_id)
            return _apply_target_cell(cell_id, mode, price_label, reason_name, cfg, state)
    
    # No threshold matched, use default
    LOG.warning("Price mode: No threshold matched for %s ($%.2f), using default", card.name, price_value)
    if mode.default_cell:
        return _apply_target_cell(mode.default_cell, mode, f"${price_value:.2f}", reason_name, cfg, state)
    
    return None


def _resolve_mode_target(
    card: Card,
    cfg: Config,
    state: SystemState,
    mode: SortMode,
    reason_name: str,
) -> Optional[Tuple[str, str]]:
    if mode.type == "alpha":
        return _assign_alpha_mode(card, mode, reason_name, cfg, state)
    if mode.type == "year":
        return _assign_year_mode(card, mode, reason_name, cfg, state)
    if mode.type == "set":
        return _assign_set_mode(card, mode, reason_name, cfg, state)
    if mode.type == "price":
        return _assign_price_mode(card, mode, reason_name, cfg, state)
    return None

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
    # helper to record scan and return
    def _do_return(cell_id: str, reason: str) -> Tuple[str, str]:
        try:
            _append_scan_csv(card, cell_id, reason)
        except Exception:
            LOG.debug("Failed to append scan CSV", exc_info=True)
        return cell_id, reason

    # 1) Confidence gate
    if card.confidence < cfg.low_conf_thresh:
        target = _overflow_target(cfg, state)
        return _do_return(target, "divert:low_confidence")

    display_name_raw = (card.display_name() or "").strip()
    reason_name = display_name_raw or (card.name.strip() if card.name else "")
    if not reason_name:
        reason_name = (card.scryfall_id or "").strip()
    if not reason_name:
        reason_name = "unknown"

    # 2) Specific sort operation mapping (if configured)
    csv_target = _resolve_sort_operation_target(card, cfg, state)
    if csv_target:
        cell_id, operation = csv_target
        cell_obj = cfg.cells.get(cell_id)
        if not cell_obj:
            LOG.warning("Sort operation target %s for card %s not in configured cells", cell_id, card.scryfall_id or card.name)
        elif _is_feeder(cell_id, cfg.feeder_re):
            LOG.warning("Sort operation target %s for card %s points to feeder; ignoring", cell_id, card.scryfall_id or card.name)
        elif _has_capacity(cell_obj, state):
                return _do_return(cell_id, f"sort_op:{operation}:{reason_name}")
        else:
            LOG.info("Sort operation target %s is full; falling back to overflow", cell_id)
            overflow_id = _overflow_target(cfg, state)
            return _do_return(overflow_id, f"overflow:{operation}:{reason_name}")

    # 3) Mode-based assignment
    active_mode_id = (state.active_sort_mode or cfg.default_sort_mode or DEFAULT_SORT_MODE).lower()
    mode = cfg.sort_modes.get(active_mode_id) or cfg.sort_modes.get(DEFAULT_SORT_MODE)
    if not mode:
        mode = SortMode(id=DEFAULT_SORT_MODE, type="alpha", label="Alphabetical", mapping=cfg.letter_to_cell, default_cell=None)

    mode_result = _resolve_mode_target(card, cfg, state, mode, reason_name)
    if mode_result:
        # mode_result is a tuple (cell, reason)
        try:
            cell_id, reason = mode_result
        except Exception:
            return mode_result
        return _do_return(cell_id, reason)

    if mode.id != DEFAULT_SORT_MODE:
        fallback_mode = cfg.sort_modes.get(DEFAULT_SORT_MODE)
        if fallback_mode:
            fallback_result = _resolve_mode_target(card, cfg, state, fallback_mode, reason_name)
            if fallback_result:
                return fallback_result

    overflow_id = _overflow_target(cfg, state)
    return _do_return(overflow_id, f"overflow:{mode.id}:{reason_name}")


def _resolve_sort_operation_target(card: Card, cfg: Config, state: SystemState) -> Optional[Tuple[str, str]]:
    if not card.scryfall_id or not cfg.sort_operations:
        return None
    op_preference: List[str] = []
    if state.active_sort_operation:
        op_preference.append(state.active_sort_operation.lower())
    if cfg.default_sort_operation and cfg.default_sort_operation not in op_preference:
        op_preference.append(cfg.default_sort_operation)
    if DEFAULT_SORT_OPERATION not in op_preference:
        op_preference.append(DEFAULT_SORT_OPERATION)
    # fallback to any available operation as last resort
    for op in cfg.sort_operations.keys():
        if op not in op_preference:
            op_preference.append(op)

    lookup_id = card.scryfall_id.lower()
    for op in op_preference:
        mapping = cfg.sort_operations.get(op)
        if not mapping:
            continue
        cell_id = mapping.get(lookup_id)
        if cell_id:
            return cell_id, op
    return None
