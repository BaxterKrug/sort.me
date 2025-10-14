
import yaml, json
from services.assign import load_config, Config, SystemState, Card, assign_card

with open("config.yaml","r") as f:
    CFG: Config = load_config(yaml.safe_load(f))

# Initialize empty counts
state = SystemState(counts_by_cell={cid: 0 for cid in CFG.cells})

def place(name, conf=1.0):
    card = Card(game="mtg", name=name, confidence=conf)
    cell, reason = assign_card(card, CFG, state)
    state.counts_by_cell[cell] = state.counts_by_cell.get(cell, 0) + 1
    return cell, reason

results = []

# 1) Basic A→B1
results.append(("A basic", place("Ancestral Recall")))

# 2) Non A–Z defaults to A→B1
results.append(("# default A", place("★Foil Card")))

# 3) Low confidence goes to ERR1
results.append(("Low conf", place("Birds of Paradise", conf=0.5)))

# 4) Fill B1 capacity=2 then overflow to ERR1
# B1 has 1 from test (A basic) and 1 from # default A; now a third 'A' should overflow
results.append(("Overflow A", place("Alpha Authority")))

# 5) C → B3
results.append(("C mapping", place("Counterspell")))

# 6) Z → J2
results.append(("Z mapping", place("Zurzoth, Chaos Rider")))

# 7) Ensure no feeder targets (assert in code). Try naming that would never hit A-row explicitly.
results.append(("Feeder bypass check", place("Island")))  # I -> D3

# 8) Capacity accounting sanity: verify counts on used cells
counts_snapshot = {k:v for k,v in state.counts_by_cell.items() if v}
results.append(("Counts", counts_snapshot))

for label, data in results:
    print(f"{label}: {data}")
