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
