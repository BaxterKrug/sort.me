# app/main.py (or similar)
from fastapi import FastAPI
import yaml
from services.assign import load_config, SystemState, Card, assign_card

app = FastAPI()

CFG = load_config(yaml.safe_load(open("config.yaml")))
STATE = SystemState(counts_by_cell={cid: 0 for cid in CFG.cells})

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
