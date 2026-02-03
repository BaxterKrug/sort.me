# sort.me

**Automated Magic: The Gathering Card Sorting System**

A FastAPI-based web application for automatically sorting Magic: The Gathering cards using computer vision, OCR, and motion control. The system captures card images, identifies them using advanced text matching and embeddings, and physically sorts them into designated bins using CNC/3D printer-style motion hardware.

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [System Requirements](#system-requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Application](#running-the-application)
- [Hardware Setup](#hardware-setup)
- [Card Identification](#card-identification)
- [Testing & Development](#testing--development)
- [Troubleshooting](#troubleshooting)
- [Architecture](#architecture)

---

## Overview

sort.me automates the process of sorting Magic: The Gathering cards by:

1. **Capturing** card images via webcam/camera
2. **Processing** images with OCR (Tesseract or EasyOCR) to extract text
3. **Identifying** cards using string matching and semantic embeddings
4. **Assigning** cards to bins based on configurable sorting modes (alphabetical, price, etc.)
5. **Moving** a vacuum pickup/plunger mechanism to physically sort cards into bins
6. **Tracking** sort sessions with Excel/CSV export for inventory management

The system includes a web-based control interface for monitoring camera feed, controlling motion, and managing sort operations.

---

## Features

### Core Functionality
- **Automated Card Recognition**: OCR-based text extraction with dual-engine support (Tesseract + EasyOCR fallback)
- **Intelligent Identification**: Lightweight string matching with semantic embedding fallback for accuracy
- **Flexible Sorting Modes**: Alphabetical, price-based, or custom sorting rules
- **Motion Control**: G-code based motion system compatible with CNC/3D printer controllers
- **Session Tracking**: Excel workbook export with per-session worksheets and metadata
- **Web Interface**: Real-time camera feed, motion jogging, and sort control

### Hardware Modes
- **Real Hardware Mode**: Full integration with camera and G-code motion controller
- **Fake Hardware Mode**: Simulated camera and motion for development without physical hardware
- **Demo Mode**: Virtual motion driver for testing sort logic without hardware

### Advanced Features
- **Feeder Monitoring**: Automatic detection of cards in feeder bins
- **Auto-sort Loop**: Continuous sorting with automatic feeder advancement
- **Confidence Thresholds**: Configurable handling of low-confidence identifications
- **Capacity Management**: Cell overflow detection and handling
- **Emergency Stop**: Web-based E-STOP for safety

---

## System Requirements

### Hardware (Real Mode)
- **Camera**: USB webcam or compatible camera (auto-detected)
- **Motion Controller**: Arduino/GRBL-compatible controller with serial interface
- **Vacuum System**: Vacuum pump controlled via M-codes
- **Plunger Mechanism**: Card pickup mechanism controlled via M-codes
- **CNC Frame**: 3-axis motion system (X/Y positioning, Z plunger control)

### Software
- **OS**: Linux (Ubuntu/Debian recommended), macOS, or Windows
- **Python**: 3.8 or higher
- **Tesseract OCR**: System package (optional, falls back to EasyOCR)
- **Dependencies**: See `requirements.txt`

---

## Installation

### 1. Install System Dependencies

#### Ubuntu/Debian
```bash
sudo apt update
sudo apt install -y tesseract-ocr libtesseract-dev libleptonica-dev pkg-config
```

#### macOS
```bash
brew install tesseract
```

#### Windows
Download and install Tesseract from: https://github.com/UB-Mannheim/tesseract/wiki

> **Note:** Tesseract is optional. If not installed, the system will automatically use EasyOCR (installed via pip).

### 2. Create Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# or
.venv\Scripts\activate     # Windows
```

### 3. Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

**Key dependencies:**
- `fastapi` - Web framework
- `uvicorn` - ASGI server
- `opencv-python-headless` - Image processing
- `pytesseract` - Tesseract OCR wrapper
- `easyocr` - Fallback OCR engine
- `sentence-transformers` - Card embeddings
- `faiss-cpu` - Vector similarity search
- `pandas` - Session export
- `numpy`, `scikit-learn`, `torch` - ML dependencies

### 4. Optional: Install Development Dependencies

```bash
pip install -r requirements-optional.txt
```

---

## Configuration

### Main Configuration File: `config.yaml`

The `config.yaml` file controls all aspects of the system:

#### Enable Fake Hardware Mode (for testing without hardware)

```yaml
use_fake_hardware: true  # false for real hardware
```

#### Camera Configuration

```yaml
camera:
  device: 0              # Camera index (auto-detected if omitted)
  width: 1920
  height: 1080
  fps: 30
  fallback_image: "path/to/fallback.jpg"  # Optional: used if camera unavailable
```

#### Motion/G-code Configuration

```yaml
gcode:
  port: /dev/ttyACM0     # Serial port for motion controller
  baud: 115200           # Baud rate
  mcodes:                # Custom M-codes (optional)
    vacuum_on: M100
    vacuum_off: M101
    plunger_down: M110
    plunger_up: M111

motion:
  default_speed: 1600.0   # mm/min
  rapid_speed: 2400.0     # mm/min
  homing_speed: 800.0     # mm/min
```

**Note:** If `gcode` section is omitted or `use_fake_hardware: true`, the system uses a virtual motion driver.

#### Cell/Bin Layout

```yaml
cells:
  - id: A1
    capacity: 120
    tags: [feeder]        # Cells tagged as 'feeder' are source bins
  - id: B1
    capacity: 60
    tags: []
  # ... more cells

grid:
  column_spacing: 84.0    # mm between columns
  row_spacing: 104.0      # mm between rows
  positions:
    A1: [17, 28]          # [X, Y] coordinates in mm
    B1: [123, 28]
    # ... more positions
```

#### Sorting Modes

```yaml
sorting:
  default_mode: alpha_exact
  low_confidence_threshold: 0.7
  near_full_threshold: 0.9
  modes:
    alpha_exact:
      letter_to_cell:
        A: B1
        B: B2
        # ... more mappings
```

---

## Running the Application

### Start the Server

```bash
# Activate virtual environment
source .venv/bin/activate

# Run with uvicorn
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Or use the provided script:

```bash
bash scripts/start_server.sh
```

### Access the Web Interface

Open your browser to:
```
http://localhost:8000/static/index.html
```

### API Documentation

FastAPI auto-generates interactive API docs:
```
http://localhost:8000/docs
```

---

## Hardware Setup

### Real Hardware Mode

1. **Connect Motion Controller**:
   - Connect Arduino/GRBL controller to USB
   - Identify serial port: `ls /dev/ttyACM*` or `ls /dev/ttyUSB*`
   - Update `config.yaml` with correct port

2. **Configure Camera**:
   - Connect USB camera
   - Test camera: `python test_camera_detection.py`
   - System auto-detects best camera if `device` not specified

3. **Calibrate Grid Positions**:
   - Use web interface motion controls to jog to each cell
   - Record X/Y coordinates in `config.yaml` under `grid.positions`

4. **Test Motion**:
   - Use `/motion/jog` API or web interface to test movements
   - Verify vacuum and plunger M-codes work correctly

### Fake Hardware Mode (Development)

See [FAKE_HARDWARE.md](FAKE_HARDWARE.md) for detailed instructions.

**Quick Start:**
1. Set `use_fake_hardware: true` in `config.yaml`
2. Add test card images to `Photos/` directory
3. Run normally - system simulates all hardware

**Test Script:**
```bash
python test_fake_hardware.py
```

---

## Card Identification

The system uses a two-tier identification approach:

### 1. Lightweight String Matching (Primary)

- Fast string comparison against Scryfall card database
- Matches card name, set code, and collector number from OCR
- Located in `app/services/card_id_lightweight.py`

### 2. Semantic Embeddings (Fallback)

- Uses sentence-transformers for semantic similarity
- Matches OCR text against pre-computed card embeddings
- More robust for damaged/misread cards
- Located in `app/services/card_id.py`

### Generating Card Embeddings

#### Full Scryfall Database

```bash
# Download Scryfall bulk data
wget https://api.scryfall.com/bulk-data/oracle-cards -O data/oracle-cards.json

# Generate embeddings (can take hours for full database)
python embed_scryfall.py --input data/oracle-cards.json --output data/embeddings
```

#### Single Card or Small Set

```bash
# Generate embedding for specific card
python scripts/embed_single_card.py \
  --card-id 0000419b-0bba-4488-8f7a-6194544ce91e \
  --out-dir data/embeddings/custom
```

#### Configure Embeddings Path

```yaml
# config.yaml
sorting:
  embeddings_dir: data/embeddings  # Path to embeddings directory
```

### OCR Engines

The system supports dual OCR engines with automatic fallback:

1. **Tesseract** (Primary): Fast, requires system installation
2. **EasyOCR** (Fallback): Pure Python, slower but no system deps

**OCR Processing:**
- Analyzes card image to detect text regions (name, rules, collector number)
- Passes region-specific crops to OCR with optimized settings
- Metadata saved to `data/snapshots/*_meta.json` for debugging

---

## Testing & Development

### Run Tests

```bash
# All tests
pytest

# Specific test file
pytest tests/test_card_embeddings.py

# With coverage
pytest --cov=app tests/
```

### Test Scripts

```bash
# Test camera detection
python test_camera_detection.py

# Test fake hardware mode
python test_fake_hardware.py

# Test OCR pipeline
pytest tests/test_ocr_pipeline.py
```

### Manual Testing with API

```bash
# Capture snapshot
curl -X POST http://localhost:8000/snapshot

# Start auto-sort
curl -X POST http://localhost:8000/auto-sort/start

# Check system status
curl http://localhost:8000/status
```

---

## Troubleshooting

### Camera Issues

**Camera not detected:**
- Run `python test_camera_detection.py`
- Check USB connections
- Try different camera index in config
- Enable fake hardware mode for testing

**Camera permissions:**
```bash
sudo usermod -a -G video $USER  # Add user to video group
# Log out and back in
```

### Motion Issues

**Serial port permission denied:**
```bash
sudo usermod -a -G dialout $USER
# Log out and back in
```

**G-code not responding:**
- Check baud rate matches controller
- Verify serial port is correct
- Use `/gcode/send` API to test raw commands
- Check controller firmware (GRBL, Marlin, etc.)

### OCR Issues

**No text detected:**
- Verify camera focus and lighting
- Check `data/snapshots/*_meta.json` for debug info
- Try different OCR engine (Tesseract vs EasyOCR)
- Adjust image preprocessing in `app/services/ocr_pipeline.py`

**Low confidence identifications:**
- Generate card embeddings for your collection
- Increase `low_confidence_threshold` in config
- Review OCR text in snapshot metadata
- Improve camera positioning and lighting

### Python Dependencies

**ImportError:**
```bash
pip install -r requirements.txt --force-reinstall
```

**PyTorch/CUDA issues:**
- System uses CPU-only PyTorch by default
- For GPU: Install appropriate torch version for your CUDA

---

## Architecture

### Directory Structure

```
sort.me/
├── app/
│   ├── services/           # Core business logic
│   │   ├── assign.py       # Card-to-cell assignment logic
│   │   ├── camera.py       # Camera management
│   │   ├── card_id.py      # Embedding-based identification
│   │   ├── card_id_lightweight.py  # String-based identification
│   │   ├── embeddings.py   # Embedding utilities
│   │   ├── feeder_monitor.py  # Feeder detection
│   │   ├── identify_assign.py  # Combined identify+assign
│   │   ├── motion.py       # Motion control (G-code)
│   │   ├── ocr_pipeline.py # OCR processing
│   │   ├── run_loop.py     # Auto-sort loop
│   │   └── sort_session.py # Session tracking/export
│   └── static/             # Web interface
│       ├── index.html
│       ├── app.js
│       └── style.css
├── data/
│   ├── embeddings/         # Pre-computed card embeddings
│   ├── snapshots/          # Captured card images + metadata
│   └── *.json, *.csv       # Session data and card database
├── scripts/                # Utility scripts
├── tests/                  # Unit tests
├── config.yaml             # Main configuration
├── main.py                 # FastAPI application
└── requirements.txt        # Python dependencies
```

### Key Components

- **FastAPI (main.py)**: REST API and web server
- **Camera Service**: Frame capture with fallback support
- **Motion Service**: G-code generation and serial communication
- **OCR Pipeline**: Image preprocessing and text extraction
- **Card Identification**: String matching + embedding search
- **Assignment Logic**: Sorting rules and bin selection
- **Session Manager**: Excel/CSV export and run tracking

---

## Advanced Topics

### Custom Sorting Modes

Add custom sorting logic in `app/services/assign.py`:

```python
def custom_sort_mode(card: Card, cfg: Config, state: SystemState) -> Tuple[str, str]:
    # Your custom logic here
    return cell_id, reason
```

Register in config:
```yaml
sorting:
  modes:
    custom:
      # your mode config
```

### Environment Variables

- `SORT_CARD_EMBED_MODEL`: Override default embedding model
- `SORT_CARD_EMBED_RUNTIME_BUILD`: Enable runtime embedding generation
- `SORTME_FORCE_VIRTUAL_DRIVER`: Force virtual motion driver

### API Endpoints (Key Examples)

- `POST /snapshot` - Capture and identify card
- `POST /auto-sort/start` - Start automated sorting
- `POST /motion/jog` - Manual motion control
- `POST /motion/home` - Home all axes
- `POST /session/start` - Begin sort session
- `GET /status` - System status
- See `/docs` for complete API reference

---

## Contributing

When developing new features:

1. Use `use_fake_hardware: true` for testing
2. Add tests in `tests/`
3. Update this README if adding new features
4. Test with real hardware before committing

---

## License

[Add your license information here]

---

## Support

For issues, questions, or contributions:
- GitHub Issues: [Add your repo URL]
- Documentation: This README + inline code comments
- API Docs: http://localhost:8000/docs (when running)
