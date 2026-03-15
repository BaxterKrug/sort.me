# QR Code Feeder Markers Setup Guide

This guide explains how to use QR codes to automatically detect when a feeder cell is empty.

## Overview

Place QR codes at the bottom of your feeder cells. When the card feeder runs out of cards, the camera will detect the QR code and automatically advance to the next feeder cell.

## Quick Start

### 1. Generate QR Codes

Run the test script to generate QR codes for your feeders:

```bash
python test_qr_detection.py --mode generate
```

This creates QR code images in the `qr_codes/` directory:
- `A1_end_marker.png` - Standard size QR code for feeder A1
- `A1_end_marker_large.png` - Large version for printing
- Similar files for A2, A3, etc.

### 2. Print and Place QR Codes

1. Print the QR codes (use the `_large.png` versions for better detection)
2. Cut them to size to fit in the bottom of your feeder cells
3. Place one QR code at the bottom of each feeder cell (A1, A2, A3)
4. Ensure the QR codes are visible to the camera when the feeder is empty

**Important:** The QR code should only become visible when all cards are depleted!

### 3. Configure Detection Regions

Edit `config.yaml` to define where to look for QR codes in the camera frame:

```yaml
camera:
  qr_codes:
    - cell: A1
      roi: [0.0, 0.75, 0.33, 1.0]  # [x0, y0, x1, y1] - bottom-left third
      history: 3  # Require 3 consecutive detections
      expected_data: "FEEDER_A1_END"  # Optional: verify QR content
    
    - cell: A2
      roi: [0.33, 0.75, 0.66, 1.0]  # bottom-middle third
      history: 3
      expected_data: "FEEDER_A2_END"
    
    - cell: A3
      roi: [0.66, 0.75, 1.0, 1.0]  # bottom-right third
      history: 3
      expected_data: "FEEDER_A3_END"
```

**ROI Coordinates:**
- Values between 0 and 1 are normalized (fraction of frame size)
- Format: `[x0, y0, x1, y1]` where (x0, y0) is top-left, (x1, y1) is bottom-right
- Example: `[0.0, 0.75, 0.33, 1.0]` = left third, bottom quarter of frame

**History Setting:**
- Number of consecutive detections required before considering QR code "stable"
- Higher values = more reliable but slower response
- Recommended: 3-5 frames

**Expected Data (Optional):**
- If specified, only QR codes with matching content will trigger
- Prevents false positives from other QR codes
- Can be omitted to accept any QR code

### 4. Test Detection

#### Test with Live Camera

View real-time QR detection with ROI guides:

```bash
python test_qr_detection.py --mode live --camera 0
```

This shows:
- Live camera feed
- QR detection results
- ROI boundaries for each feeder
- Press 'q' to quit, 's' to save frame

#### Test with Saved Image

Test detection on a captured image:

```bash
python test_qr_detection.py --mode detect --image path/to/image.jpg
```

### 5. API Endpoints

Once configured, you can access QR scanner functionality via the API:

#### Get Scanner Status
```bash
curl http://localhost:8000/qr/status
```

Returns configuration and last scan results.

#### Scan Now
```bash
curl http://localhost:8000/qr/scan
# Or scan specific cells:
curl http://localhost:8000/qr/scan?cells=A1,A2
```

#### Reset Detection History
```bash
curl -X POST http://localhost:8000/qr/reset
# Or reset specific cell:
curl -X POST http://localhost:8000/qr/reset -H "Content-Type: application/json" -d '{"cell": "A1"}'
```

## How It Works

### Detection Flow

1. **Camera Monitoring**: During normal operation, the QR scanner periodically checks configured ROI regions
2. **QR Detection**: When a QR code appears in a feeder ROI, it's detected and decoded
3. **History Tracking**: Detection must be stable (consistent across multiple frames) to avoid false positives
4. **Feeder Advance**: When a stable QR detection occurs, the system:
   - Marks that feeder as empty (sets count to 0)
   - Advances to the next feeder in sequence
   - Logs the event with QR code data

### Integration with Feeder Monitor

The QR scanner works alongside the visual fill detection:

- **Visual Detection**: Analyzes pixel brightness to estimate card presence
- **QR Detection**: Confirms feeder is empty when QR code is visible
- **Combined Logic**: Either method can trigger feeder advance

This dual approach provides redundancy and reliability.

## Troubleshooting

### QR Code Not Detected

1. **Check Camera View**: Use live test mode to verify QR code is visible
2. **Adjust ROI**: Make sure ROI covers the QR code location
3. **Improve Lighting**: Ensure adequate lighting for clear QR code visibility
4. **Increase Size**: Use larger QR codes for better detection at distance
5. **Reduce History**: Lower the `history` value for faster detection

### False Positives

1. **Verify Expected Data**: Set `expected_data` to only accept specific QR codes
2. **Increase History**: Require more consecutive detections (5-7 frames)
3. **Adjust ROI**: Narrow the ROI to exclude other objects

### QR Code Partially Visible

- Ensure QR codes have adequate border (white space around them)
- Use high error correction level (already set in generated codes)
- Position QR codes flat and perpendicular to camera

## Custom QR Codes

To create your own QR codes with custom data:

```python
import qrcode

qr = qrcode.QRCode(
    version=1,
    error_correction=qrcode.constants.ERROR_CORRECT_H,
    box_size=10,
    border=4,
)
qr.add_data("YOUR_CUSTOM_DATA_HERE")
qr.make(fit=True)

img = qr.make_image(fill_color="black", back_color="white")
img.save("custom_qr.png")
```

## Advanced Configuration

### Multiple Feeders

For more than 3 feeders, add additional entries to `camera.qr_codes`:

```yaml
camera:
  qr_codes:
    - cell: A4
      roi: [0.0, 0.5, 0.25, 0.75]
      history: 3
      expected_data: "FEEDER_A4_END"
```

### Pixel Coordinates

Instead of normalized coordinates, you can use absolute pixel values:

```yaml
camera:
  qr_codes:
    - cell: A1
      roi: [0, 540, 426, 720]  # Pixels for 1280x720 frame
```

### Different QR Content

QR codes can contain any data. Examples:
- `"EMPTY"` - Simple message
- `"A1_END"` - Cell identifier
- `"123456"` - Numeric ID
- `"https://example.com/feeder/a1"` - URL

Just update `expected_data` to match your QR code content, or omit it to accept any QR code.

### QR Code Commands

QR codes can contain commands that execute specific actions when scanned.

**Supported Command Format:**
```
[CELL_ID] [COMMAND]
```

**Available Commands:**

| Command | Example QR Data | Action |
|---------|-----------------|--------|
| `endstep` | `A1 endstep` | Marks feeder as empty and advances to next feeder |
| `refill` | `A1 refill` | Marks feeder as refilled with full capacity |
| `pause` | `A1 pause` | Pauses the sorting operation |
| `resume` | `A1 resume` | Resumes the sorting operation |

**Examples:**

```
A1 endstep    → Mark A1 as empty, advance to A2
A1            → Same as "A1 endstep" (default command)
endstep       → Mark current detected cell as empty
A2 refill     → Mark A2 as refilled
pause         → Pause sorting
```

**Command Deduplication:**
- Each QR code is only executed once per detection cycle
- When a feeder is refilled and the QR becomes hidden, then visible again, the command will re-execute
- Use `reset_qr_command_state()` in code to force re-execution

## Tips

1. **Print Quality**: Use high-quality printing for crisp QR codes
2. **Lamination**: Laminate QR codes to protect from wear
3. **Contrast**: Ensure high contrast (black on white) for best detection
4. **Positioning**: Place QR codes centered in the expected ROI area
5. **Testing**: Test each feeder position before production use
6. **Backups**: Keep digital copies of your QR codes for reprinting

## Support

If you encounter issues:

1. Check logs for QR scanner messages: `grep "sort.qr_scanner" logs/`
2. Review camera feed to verify QR code visibility
3. Test with the standalone test script first
4. Verify OpenCV installation: `python -c "import cv2; print(cv2.__version__)"`
