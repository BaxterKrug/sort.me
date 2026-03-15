"""QR Code scanner for detecting feeder end-of-stack markers.

This service detects a single QR code in camera frames, typically used to mark
the bottom of feeder cells. QR codes contain commands like "A1 endstep" to
signal that a feeder is empty and the machine should advance to the next one.

Simplified design: expects ONE QR code filling most of the frame.
"""

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import cv2  # type: ignore
import numpy as np

from . import camera

LOG = logging.getLogger("sort.qr_scanner")


class QRScanner:
    """Detects a single QR code in camera frames (full-frame scan)."""

    def __init__(self, history_length: int = 3, enabled: bool = True) -> None:
        """Initialize the QR scanner.
        
        Args:
            history_length: Number of consecutive detections required for stable detection.
            enabled: Whether the scanner is enabled.
        """
        self.history_length = history_length
        self._history: Deque[Optional[str]] = deque(maxlen=history_length)
        self.last_result: Dict[str, Any] = {}
        self.enabled = enabled
        self._last_stable_data: Optional[str] = None
        
        # Initialize QR code detector
        try:
            self._qr_detector = cv2.QRCodeDetector()
            LOG.info("QR code detector initialized (full-frame mode)")
        except Exception as e:
            LOG.error("Failed to initialize QR detector: %s", e)
            self.enabled = False

    async def scan(self, frame: Optional[np.ndarray] = None) -> Dict[str, Any]:
        """Scan the full frame for a QR code.
        
        Args:
            frame: Optional camera frame. If None, grabs a fresh frame from camera.
            
        Returns:
            Dictionary with detection results:
                - detected: True if QR code was found
                - data: The QR code data string (e.g., "A1 endstep")
                - stable: True if same data detected history_length consecutive times
                - corners: Corner points of the detected QR code
                - cell: Parsed cell ID from the QR data (if applicable)
                - command: Parsed command from the QR data (if applicable)
        """
        if not self.enabled:
            return {"detected": False, "data": None, "stable": False}
        
        if frame is None:
            try:
                frame = await camera.get_manager().grab_frame()
            except Exception as exc:
                LOG.warning("Failed to grab camera frame: %s", exc)
                return {"detected": False, "data": None, "stable": False, "error": str(exc)}
        
        # Detect QR code in the full frame
        detected, data, corners = self._detect_qr(frame)
        
        # Update detection history
        self._history.append(data if detected else None)
        
        # Check for stable detection (same data N times in a row)
        stable = False
        if detected and data:
            # All history entries must match the current data
            stable = (
                len(self._history) == self.history_length and
                all(h == data for h in self._history)
            )
        
        # Parse cell and command from QR data
        cell, command = self._parse_qr_data(data) if data else (None, None)
        
        # Track if this is a NEW stable detection (wasn't stable before)
        is_new_stable = stable and (self._last_stable_data != data)
        if stable:
            self._last_stable_data = data
        elif not detected:
            # Reset stable tracking when QR code disappears
            self._last_stable_data = None
        
        result = {
            "detected": detected,
            "data": data,
            "stable": stable,
            "is_new_stable": is_new_stable,
            "corners": corners.tolist() if corners is not None else None,
            "cell": cell,
            "command": command,
            "history_count": sum(1 for h in self._history if h == data) if data else 0,
            "required_count": self.history_length,
        }
        
        self.last_result = result
        
        # Debug logging for QR detection progress
        if detected:
            history_count = result["history_count"]
            LOG.debug("QR detected: '%s' (%d/%d for stable)", data, history_count, self.history_length)
        
        if is_new_stable:
            LOG.info("QR code stable detection: '%s' (cell=%s, cmd=%s)", data, cell, command)
        
        return result

    def _detect_qr(self, image: np.ndarray) -> Tuple[bool, Optional[str], Optional[np.ndarray]]:
        """Detect QR code in an image.
        
        Args:
            image: Image to scan.
            
        Returns:
            Tuple of (detected, data, corners).
        """
        if image is None or image.size == 0:
            LOG.warning("QR detect: image is None or empty")
            return False, None, None
        
        LOG.debug("QR detect: image shape=%s, dtype=%s", image.shape, image.dtype)
        
        try:
            # Convert to grayscale for better detection
            if len(image.shape) == 3:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            else:
                gray = image
            
            # Detect and decode QR code
            data, corners, _ = self._qr_detector.detectAndDecode(gray)
            
            if data:
                LOG.info("QR detect: found '%s' on first try", data.strip())
                return True, data.strip(), corners
            
            # Try with some preprocessing if initial detection fails
            # Apply adaptive thresholding for better contrast
            thresh = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
            )
            data, corners, _ = self._qr_detector.detectAndDecode(thresh)
            
            if data:
                LOG.info("QR detect: found '%s' with adaptive threshold", data.strip())
                return True, data.strip(), corners
            
            LOG.debug("QR detect: no QR code found in frame")
            return False, None, None
            
        except Exception as e:
            LOG.debug("QR detection error: %s", e)
            return False, None, None

    def _parse_qr_data(self, qr_data: str) -> Tuple[Optional[str], Optional[str]]:
        """Parse cell ID and command from QR code data.
        
        Expected formats:
            - "A1 endstep" -> cell="A1", command="endstep"
            - "A1" -> cell="A1", command="endstep" (default)
            - "endstep" -> cell=None, command="endstep"
            - "FEEDER_A1_END" -> cell="A1", command="endstep" (legacy format)
            - "FEEDER_A1_REFILL" -> cell="A1", command="refill" (legacy format)
        
        Args:
            qr_data: Raw QR code data string.
            
        Returns:
            Tuple of (cell, command).
        """
        if not qr_data:
            return None, None
        
        data_upper = qr_data.strip().upper()
        
        # Handle legacy "FEEDER_XX_END" or "FEEDER_XX_REFILL" format
        feeder_pattern = re.compile(r'^FEEDER_([A-Z]+\d+)_(\w+)$')
        match = feeder_pattern.match(data_upper)
        if match:
            cell = match.group(1)
            cmd_word = match.group(2).lower()
            # Map legacy command names to new ones
            if cmd_word in ("end", "endstep", "empty"):
                command = "endstep"
            elif cmd_word in ("refill", "reload", "full"):
                command = "refill"
            else:
                command = cmd_word
            LOG.debug("Parsed legacy QR format: FEEDER_%s_%s -> cell=%s, command=%s", 
                     cell, cmd_word.upper(), cell, command)
            return cell, command
        
        parts = data_upper.split()
        cell = None
        command = "endstep"  # Default command
        
        if not parts:
            return None, None
        
        # Cell pattern: letter(s) + number(s), e.g., A1, B12, AA1
        cell_pattern = re.compile(r'^[A-Z]+\d+$')
        
        if cell_pattern.match(parts[0]):
            cell = parts[0]
            if len(parts) > 1:
                command = parts[1].lower()
        else:
            # First part is probably a command
            command = parts[0].lower()
            if len(parts) > 1 and cell_pattern.match(parts[1]):
                cell = parts[1]
        
        return cell, command

    def reset_history(self) -> None:
        """Reset detection history."""
        self._history.clear()
        self._last_stable_data = None
        LOG.debug("Reset QR detection history")

    def is_stable_cleared(self) -> bool:
        """Check if stable detection state has been cleared (QR disappeared).
        
        Returns:
            True if no QR code is currently being tracked as stable.
        """
        return self._last_stable_data is None


# Legacy compatibility: QRRegion dataclass for old config format
@dataclass
class QRRegion:
    """Legacy: Defines a QR code detection configuration for a feeder cell."""
    cell: str
    expected_data: Optional[str] = None
    history: int = 3


# Module-level scanner instance
_scanner: Optional[QRScanner] = None


def configure(history_length: int = 3, enabled: bool = True) -> QRScanner:
    """Configure and return the global QR scanner.
    
    Args:
        history_length: Number of consecutive detections for stable detection.
        enabled: Whether the scanner should be enabled.
        
    Returns:
        The configured QRScanner instance.
    """
    global _scanner
    _scanner = QRScanner(history_length=history_length, enabled=enabled)
    LOG.info("QR scanner configured: history=%d, enabled=%s", history_length, enabled)
    return _scanner


def configure_from_cfg(cfg: Any, camera_cfg: Optional[Dict[str, Any]]) -> Optional[QRScanner]:
    """Configure QR scanner from config dictionaries.
    
    Args:
        cfg: Main configuration object.
        camera_cfg: Camera configuration dictionary.
        
    Returns:
        The configured QRScanner instance.
    """
    global _scanner
    
    camera_cfg = camera_cfg or {}
    qr_cfg = camera_cfg.get("qr_codes") if isinstance(camera_cfg, dict) else None
    
    # Default: enable scanner with history=3
    history = 3
    enabled = True
    
    if qr_cfg:
        # If qr_codes is a list, extract history from first entry
        if isinstance(qr_cfg, list) and qr_cfg:
            first = qr_cfg[0] if qr_cfg else {}
            history = int(first.get("history", 3))
        elif isinstance(qr_cfg, dict):
            history = int(qr_cfg.get("history", 3))
            enabled = qr_cfg.get("enabled", True)
    
    _scanner = QRScanner(history_length=history, enabled=enabled)
    LOG.info("QR scanner configured from cfg: history=%d, enabled=%s (full-frame mode)", history, enabled)
    return _scanner


def get_scanner() -> Optional[QRScanner]:
    """Get the module-level QR scanner instance.
    
    Returns:
        The configured QRScanner instance, or None if not configured.
    """
    global _scanner
    if _scanner is None:
        # Auto-configure with defaults
        _scanner = QRScanner(history_length=3, enabled=True)
    return _scanner


async def scan_qr(frame: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """Convenience function to scan for QR code.
    
    Args:
        frame: Optional camera frame.
        
    Returns:
        Detection result dictionary.
    """
    scanner = get_scanner()
    if scanner is None:
        return {"detected": False, "data": None, "stable": False, "error": "Scanner not configured"}
    return await scanner.scan(frame)
