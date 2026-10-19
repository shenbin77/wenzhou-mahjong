"""Direct ADB transport with lossless raw screenshots and explicit failures.

Raw screencap avoids Android's PNG encoding cost on the local emulator. It
transfers more bytes, so use capture_format="png" explicitly for slower links.
No retries, fallback capture modes, or persistent device services are started.
"""
from __future__ import annotations

import math
import os
import struct
import subprocess
from typing import Literal

import cv2
import numpy as np


class AdbTransportError(RuntimeError):
    """ADB command, process lifecycle, or screenshot decoding failed."""


class CaptureDecodeError(AdbTransportError):
    """The screenshot does not match its declared format or dimensions."""


def decode_raw_screencap(data: bytes) -> np.ndarray:
    """Decode Android's 12/16-byte raw screencap header into contiguous BGR.

    Header fields are little-endian width, height and Android pixel format;
    newer Android versions append a uint32 colorspace field. Screencap writes
    tightly packed rows. Payload length decides the header version exactly.
    """
    if len(data) < 12:
        raise CaptureDecodeError(f"raw screencap header truncated: {len(data)} bytes")
    width, height, pixel_format = struct.unpack_from("<III", data)
    if not (0 < width <= 16384 and 0 < height <= 16384) or width * height > 64_000_000:
        raise CaptureDecodeError(f"invalid raw screencap dimensions: {width}x{height}")
    # HAL_PIXEL_FORMAT: RGBA_8888, RGBX_8888, RGB_888, RGB_565, BGRA_8888.
    bytes_per_pixel = {1: 4, 2: 4, 3: 3, 4: 2, 5: 4}.get(pixel_format)
    if bytes_per_pixel is None:
        raise CaptureDecodeError(f"unsupported raw screencap pixel format: {pixel_format}")
    payload_size = width * height * bytes_per_pixel
    header_size = len(data) - payload_size
    if header_size not in (12, 16):
        raise CaptureDecodeError(
            f"raw screencap size mismatch: {width}x{height}, format={pixel_format}, "
            f"received={len(data)}, expected={payload_size + 12} or {payload_size + 16}"
        )
    pixels = np.frombuffer(data, dtype=np.uint8, offset=header_size)
    pixels = pixels.reshape(height, width, bytes_per_pixel)
    color_code = {
        1: cv2.COLOR_RGBA2BGR,
        2: cv2.COLOR_RGBA2BGR,
        3: cv2.COLOR_RGB2BGR,
        4: cv2.COLOR_BGR5652BGR,
        5: cv2.COLOR_BGRA2BGR,
    }[pixel_format]
    return cv2.cvtColor(pixels, color_code)


def decode_png(data: bytes) -> np.ndarray:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CaptureDecodeError("screencap did not return a PNG signature")
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise CaptureDecodeError(f"PNG screenshot decoding failed: {len(data)} bytes")
    return image


class AdbTransport:
    """A screenshot/input connection scoped to one explicitly selected device."""

    def __init__(
        self,
        serial: str,
        adb_path: str | os.PathLike[str] = "adb",
        *,
        capture_format: Literal["raw", "png"] = "raw",
        capture_timeout: float = 15,
        tap_timeout: float = 15,
        connection_timeout: float = 5,
    ):
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("serial must explicitly identify the target ADB device")
        if capture_format not in ("raw", "png"):
            raise ValueError("capture_format must be 'raw' or 'png'")
        for name, value in (("capture_timeout", capture_timeout), ("tap_timeout", tap_timeout),
                            ("connection_timeout", connection_timeout)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        self.serial = serial
        self.adb_path = os.fspath(adb_path)
        self.capture_format = capture_format
        self.capture_timeout = capture_timeout
        self.tap_timeout = tap_timeout
        self.connection_timeout = connection_timeout
        self._closed = False
        self._frame_size: tuple[int, int] | None = None

    def _run(self, args: list[str], timeout: float, *, check: bool = True):
        if self._closed:
            raise AdbTransportError("ADB transport is closed")
        command = [self.adb_path, "-s", self.serial, *args]
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise AdbTransportError(f"ADB executable not found: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbTransportError(f"ADB {' '.join(args)} timed out after {timeout}s") from exc
        except OSError as exc:
            raise AdbTransportError(f"Could not execute ADB: {exc}") from exc
        if check and result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:400]
            raise AdbTransportError(f"ADB {' '.join(args)} failed ({result.returncode}): {detail}")
        return result

    def is_connected(self) -> bool:
        result = self._run(["get-state"], self.connection_timeout, check=False)
        return result.returncode == 0 and result.stdout.strip() == b"device"

    def capture(self) -> np.ndarray:
        args = ["exec-out", "screencap"]
        if self.capture_format == "png":
            args.append("-p")
        data = self._run(args, self.capture_timeout).stdout
        image = decode_raw_screencap(data) if self.capture_format == "raw" else decode_png(data)
        self._frame_size = (image.shape[1], image.shape[0])
        return image

    def capture_png(self, frame: np.ndarray | None = None) -> bytes:
        """Encode a supplied BGR frame for logging, or capture a new frame.

        Passing the frame used by recognition preserves evidence of that exact
        observation and avoids issuing a second capture while the game changes.
        """
        if self._closed:
            raise AdbTransportError("ADB transport is closed")
        if frame is None:
            frame = self.capture()
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 \
                or frame.shape[2] != 3 or not frame.size:
            raise ValueError("frame must be a nonempty uint8 BGR image")
        ok, encoded = cv2.imencode(".png", frame, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        if not ok:
            raise CaptureDecodeError("could not encode screenshot as PNG")
        return encoded.tobytes()

    def tap(self, x: int, y: int) -> None:
        if not math.isfinite(x) or not math.isfinite(y) or x < 0 or y < 0:
            raise ValueError("tap coordinates must be finite and nonnegative")
        x, y = int(x), int(y)
        if self._frame_size and (x >= self._frame_size[0] or y >= self._frame_size[1]):
            raise ValueError(f"tap ({x}, {y}) outside last capture {self._frame_size}")
        self._run(["shell", "input", "tap", str(x), str(y)], self.tap_timeout)

    def close(self) -> None:
        # No persistent shell or device process is owned by this transport.
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
