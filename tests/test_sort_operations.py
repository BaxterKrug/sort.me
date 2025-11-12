from typing import Dict, Any

from app.services.assign import Card, SystemState, assign_card, load_config


def _build_config(csv_path: str) -> Dict[str, Any]:
    return {
        "cells": [
            {"id": "A1", "capacity": 10, "tags": []},
            {"id": "B1", "capacity": 1, "tags": []},
            {"id": "ERR1", "capacity": 10, "tags": []},
        ],
        "alpha_exact": {
            "letter_to_cell": {
                "A": "A1",
                "B": "B1",
            }
        },
        "overflow": {"cells": ["ERR1"]},
        "sorting": {
            "operations_csv": csv_path,
            "default_sort_operation": "binder1",
        },
    }


def test_assign_card_prefers_sort_operation(tmp_path):
    csv_path = tmp_path / "sort_ops.csv"
    csv_path.write_text("operation,scryfall_id,target_cell\nbinder1,abc123,B1\n", encoding="utf8")

    cfg = load_config(_build_config(str(csv_path)))
    state = SystemState(counts_by_cell={cid: 0 for cid in cfg.cells})
    state.active_sort_operation = cfg.default_sort_operation
    state.active_sort_mode = cfg.default_sort_mode

    card = Card(game="mtg", name="Ancestral Recall", scryfall_id="ABC123", confidence=1.0)
    cell, reason = assign_card(card, cfg, state)
    assert cell == "B1"
    assert reason.startswith("sort_op:binder1")
    assert reason.endswith("Ancestral Recall")

    fallback = Card(game="mtg", name="Alpha Strike", scryfall_id="zzz999", confidence=1.0)
    fallback_cell, fallback_reason = assign_card(fallback, cfg, state)
    assert fallback_cell == "A1"
    assert fallback_reason.startswith("alpha_exact")
    assert fallback_reason.endswith("Alpha Strike")

    printed_pref = Card(
        game="mtg",
        name="Beta Strike",
        printed_name="Alpha Strike",
        scryfall_id="zzz998",
        confidence=1.0,
    )
    printed_cell, printed_reason = assign_card(printed_pref, cfg, state)
    assert printed_cell == "A1"
    assert printed_reason.startswith("alpha_exact")
    assert printed_reason.endswith("Alpha Strike")

    state.counts_by_cell["B1"] = cfg.cells["B1"].capacity
    overflow_card = Card(game="mtg", name="Ancestral Recall", scryfall_id="abc123", confidence=1.0)
    overflow_cell, overflow_reason = assign_card(overflow_card, cfg, state)
    assert overflow_cell == "ERR1"
    assert overflow_reason.startswith("overflow")


def _build_mode_config() -> Dict[str, Any]:
    return {
        "cells": [
            {"id": "A1", "capacity": 10, "tags": []},
            {"id": "B1", "capacity": 10, "tags": []},
            {"id": "B2", "capacity": 10, "tags": []},
            {"id": "B3", "capacity": 10, "tags": []},
            {"id": "C1", "capacity": 10, "tags": []},
            {"id": "ERR1", "capacity": 10, "tags": []},
        ],
        "alpha_exact": {
            "letter_to_cell": {
                "A": "A1",
            }
        },
        "overflow": {"cells": ["ERR1"]},
        "sorting": {
            "default_mode": "release_year",
            "modes": {
                "release_year": {
                    "type": "year",
                    "label": "Release Year",
                    "fallback": "B3",
                    "mapping": {
                        "2021": "B1",
                        "2020s": "B2",
                        "*": "B3",
                    },
                },
                "set_focus": {
                    "type": "set",
                    "label": "Set Focus",
                    "fallback": "C1",
                    "mapping": {
                        "mh1": "B1",
                        "modern horizons": "B1",
                        "*": "C1",
                    },
                },
            },
        },
    }


def test_assign_card_respects_year_mode():
    cfg = load_config(_build_mode_config())
    state = SystemState(counts_by_cell={cid: 0 for cid in cfg.cells})
    state.active_sort_operation = cfg.default_sort_operation
    state.active_sort_mode = "release_year"

    card_year_exact = Card(game="mtg", name="Test Card", released_year="2021", confidence=1.0)
    cell_exact, reason_exact = assign_card(card_year_exact, cfg, state)
    assert cell_exact == "B1"
    assert reason_exact.startswith("release_year:2021")

    card_year_decade = Card(game="mtg", name="Future Card", released_year="2023", confidence=1.0)
    cell_decade, reason_decade = assign_card(card_year_decade, cfg, state)
    assert cell_decade == "B2"
    assert "2020s" in reason_decade

    card_year_unknown = Card(game="mtg", name="Mystery", confidence=1.0)
    cell_unknown, reason_unknown = assign_card(card_year_unknown, cfg, state)
    assert cell_unknown == "B3"
    assert reason_unknown.startswith("release_year:*")


def test_assign_card_respects_set_mode():
    cfg = load_config(_build_mode_config())
    state = SystemState(counts_by_cell={cid: 0 for cid in cfg.cells})
    state.active_sort_operation = cfg.default_sort_operation
    state.active_sort_mode = "set_focus"

    card_code_match = Card(game="mtg", name="Horizon Card", set_code="MH1", confidence=1.0)
    cell_code, reason_code = assign_card(card_code_match, cfg, state)
    assert cell_code == "B1"
    assert reason_code.startswith("set_focus:MH1")

    card_name_match = Card(game="mtg", name="Modern Horizon", set_name="Modern Horizons", confidence=1.0)
    cell_name, reason_name = assign_card(card_name_match, cfg, state)
    assert cell_name == "B1"
    assert "MODERN HORIZONS" in reason_name

    card_set_unknown = Card(game="mtg", name="Unknown Set", confidence=1.0)
    cell_default, reason_default = assign_card(card_set_unknown, cfg, state)
    assert cell_default == "C1"
    assert reason_default.startswith("set_focus:*")
