# Fake Hardware Mode

This project includes a fake hardware mode that allows you to test and develop without real camera or motion hardware.

## Quick Start

### Enable Fake Hardware

Edit `config.yaml` and set:

```yaml
use_fake_hardware: true
```

### Disable Fake Hardware (Use Real Hardware)

Edit `config.yaml` and set:

```yaml
use_fake_hardware: false
```

## What Gets Simulated

### 1. Fake Camera
- **Source**: Randomly selects images from the `Photos/` directory
- **Behavior**: Each frame grab returns a different random image from Photos
- **Fallback**: If no images are found, generates a test pattern

### 2. Virtual Motion Driver
- **Type**: Uses `VirtualMotionDriver` instead of real G-code hardware
- **Behavior**: Simulates all motion commands with instant response
- **Features**:
  - Tracks position in memory
  - Simulates homing, moves, vacuum, plunger
  - No delays except small async sleeps for realism

## Testing

Run the test script to verify fake hardware mode is working:

```bash
python test_fake_hardware.py
```

This will:
1. Check the config setting
2. Test the fake camera (grab frames)
3. Test the virtual motion driver (move commands)
4. Report success or failure

## Adding Test Images

To use your own test images with the fake camera:

1. Place JPG, JPEG, PNG, or BMP files in the `Photos/` directory
2. The fake camera will randomly select from these images
3. Each frame grab returns a different random image

## When to Use Fake Hardware

✅ **Use fake hardware when:**
- Developing new features
- Testing sort algorithms
- Running unit tests
- Working without access to the physical machine
- Debugging card identification logic

❌ **Don't use fake hardware when:**
- Testing actual camera calibration
- Verifying motion accuracy
- Running production sorts
- Debugging hardware-specific issues

## Implementation Details

### Camera (`app/services/camera.py`)
- `FakeCamera` class mimics `cv2.VideoCapture` interface
- Implements `read()`, `isOpened()`, `release()` methods
- Returns images from `Photos/` directory randomly

### Motion (`app/services/motion.py`)
- `VirtualMotionDriver` already existed for testing
- Now automatically selected when `use_fake_hardware: true`
- Maintains virtual position state
- Logs all commands for debugging

### Configuration (`main.py`)
- Reads `use_fake_hardware` from config.yaml
- Passes flag to both camera and motion systems
- No code changes needed to switch modes

## Environment Variables

You can also force virtual motion driver using:

```bash
export SORTME_FORCE_VIRTUAL_DRIVER=1
python main.py
```

However, using `use_fake_hardware: true` in config.yaml is preferred as it enables both fake camera and motion together.

## Troubleshooting

**No images in Photos directory:**
- The fake camera will generate a test pattern instead
- Add at least one image to Photos/ for realistic testing

**Hardware still trying to connect:**
- Make sure `use_fake_hardware: true` is set in config.yaml
- Check that config.yaml is in the working directory
- Restart the application after changing config

**Tests failing:**
- Run `python test_fake_hardware.py` to diagnose
- Check the logs for detailed error messages
- Verify yaml package is installed: `pip install pyyaml`
