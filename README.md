# sort.me

Hardware / demo mode
--------------------

The server can run in two modes:

- Demo/simulated mode (safe): the system uses a LoggingDriver which prints
	the G-code that would be sent and simulates movement. This is the default
	when no `gcode` section is present in `config.yaml`.

- Real hardware mode: provide a `gcode` section in `config.yaml` with the
	serial port and baud rate for your firmware. Example:

```yaml
gcode:
	port: /dev/ttyUSB0
	baud: 115200
	mcodes:
		vacuum_on: M100
		vacuum_off: M101
		plunger_down: M110
		plunger_up: M111
```

When `gcode` options are present the server will, by default, attempt to
instantiate a GCodeDriver and talk to the motor controller. You can still
override this by setting `demo: true` in `config.yaml` to force demo mode.

At runtime you can toggle demo mode via the API:

POST /demo/mode  -> {"demo": true|false, "gcode_opts": {...}}

Development / dependencies
--------------------------

This project requires the Tesseract OCR binary (system package) and a few
Python packages. Install system Tesseract first (example for Debian/Ubuntu):

```bash
sudo apt update
sudo apt install -y tesseract-ocr libtesseract-dev libleptonica-dev pkg-config
```

Then install Python runtime deps into the project's virtualenv and pin them
using the provided `requirements.txt`:

```bash
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

If you're running in a headless server environment prefer
`opencv-python-headless` (already used in `requirements.txt`).

Card embeddings
---------------

To keep startup light, the backend will only load embeddings that already
exist on disk. If `data/embeddings/embeddings.npy` is missing, set the
`sorting.embeddings_dir` config to a directory you prepared ahead of time.

You can generate a tiny embedding set (even a single Scryfall ID) with:

```bash
python scripts/embed_single_card.py \
	--card-id 0000419b-0bba-4488-8f7a-6194544ce91e \
	--out-dir data/embeddings/custom-forest
```

Then point `config.yaml` at `data/embeddings/custom-forest` under
`sorting.embeddings_dir`. The script only processes the requested IDs or
names, so it won't hammer the CPU.

Both the single-card helper and `embed_scryfall.py` now write each card's
embedding directly into `cards_metadata.json`, so the server only needs to
generate an OCR text embedding at runtime before comparing it against those
precomputed vectors.

If you prefer to build the full Scryfall index offline, continue using
`embed_scryfall.py` (which reads the big oracle JSON export). During development
you can block runtime generation entirely by leaving
`SORT_CARD_EMBED_RUNTIME_BUILD` unset (defaults to off). Set it to `1` only if
you intentionally want the server to compute all missing embeddings at startup.

### OCR fallbacks

If Tesseract is not available, the snapshot pipeline will automatically
fallback to [EasyOCR](https://github.com/JaidedAI/EasyOCR) (installed via
`requirements.txt`). The fallback does not require additional system
packages, but it does rely on PyTorch; both CPU-only wheels are pinned in the
requirements list. When neither engine is present the API log will emit a
clear error (`No OCR engines available; install tesseract or easyocr...`).
Install at least one of the engines to see live OCR output in the UI and
metadata files.
