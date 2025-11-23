"""Manual harness for verifying assign pipeline; excluded from automated tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

import yaml

from app.services.assign import load_config, Config, SystemState, Card, assign_card


def _load_cfg() -> Config:
    with Path("config.yaml").open("r", encoding="utf8") as handle:
        return load_config(yaml.safe_load(handle))


def _build_state(cfg: Config) -> SystemState:
    state = SystemState(counts_by_cell={cid: 0 for cid in cfg.cells})
    state.active_sort_operation = cfg.default_sort_operation
    state.active_sort_mode = cfg.default_sort_mode
    return state


def place(name: str, cfg: Config, state: SystemState, conf: float = 1.0) -> Tuple[str, str]:
    card = Card(game="mtg", name=name, confidence=conf)
    cell, reason = assign_card(card, cfg, state)
    state.counts_by_cell[cell] = state.counts_by_cell.get(cell, 0) + 1
    return cell, reason


def run_demo() -> None:
    cfg = _load_cfg()
    state = _build_state(cfg)
    results = []

    results.append(("A basic", place("Ancestral Recall", cfg, state)))
    results.append(("# default A", place("★Foil Card", cfg, state)))
    results.append(("Low conf", place("Birds of Paradise", cfg, state, conf=0.5)))
    results.append(("Overflow A", place("Alpha Authority", cfg, state)))
    results.append(("C mapping", place("Counterspell", cfg, state)))
    results.append(("Z mapping", place("Zurzoth, Chaos Rider", cfg, state)))
    results.append(("Feeder bypass check", place("Island", cfg, state)))

    counts_snapshot = {k: v for k, v in state.counts_by_cell.items() if v}
    results.append(("Counts", json.dumps(counts_snapshot, indent=2)))

    for label, data in results:
        print(f"{label}: {data}")


if __name__ == "__main__":
    run_demo()
