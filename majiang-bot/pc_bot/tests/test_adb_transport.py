"""Transport tests are offline: they never connect to or click an emulator."""
import struct
import subprocess
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from pc_bot.adb_transport import AdbTransport, AdbTransportError, CaptureDecodeError
from pc_bot.adb_transport import decode_png, decode_raw_screencap


def raw_frame(pixels, pixel_format=1, colorspace=None):
    height, width, _ = pixels.shape
    header = struct.pack("<III", width, height, pixel_format)
    if colorspace is not None:
        header += struct.pack("<I", colorspace)
    return header + pixels.tobytes()


class RawCaptureTests(unittest.TestCase):
    def test_android14_header_rgba_order_and_orientation(self):
        rgba = np.array([[[255, 0, 0, 255], [0, 255, 0, 80]],
                         [[0, 0, 255, 255], [13, 10, 99, 255]]], dtype=np.uint8)
        image = decode_raw_screencap(raw_frame(rgba, colorspace=1))
        np.testing.assert_array_equal(image, rgba[:, :, [2, 1, 0]])
        self.assertTrue(image.flags.c_contiguous)
        self.assertEqual(image.dtype, np.uint8)

    def test_legacy_header_and_rgbx(self):
        rgbx = np.array([[[25, 36, 47, 0]]], dtype=np.uint8)
        np.testing.assert_array_equal(decode_raw_screencap(raw_frame(rgbx, 2)), [[[47, 36, 25]]])

    def test_rgb_and_bgra(self):
        rgb = np.array([[[13, 10, 100]]], dtype=np.uint8)
        np.testing.assert_array_equal(decode_raw_screencap(raw_frame(rgb, 3, 1)), [[[100, 10, 13]]])
        bgra = np.array([[[13, 10, 100, 200]]], dtype=np.uint8)
        np.testing.assert_array_equal(decode_raw_screencap(raw_frame(bgra, 5, 1)), [[[13, 10, 100]]])

    def test_rgb565_primaries(self):
        packed = np.array([[[0x00, 0xF8], [0xE0, 0x07], [0x1F, 0x00]]], dtype=np.uint8)
        np.testing.assert_array_equal(decode_raw_screencap(raw_frame(packed, 4, 1)),
                                      [[[0, 0, 248], [0, 252, 0], [248, 0, 0]]])

    def test_short_header_invalid_dimensions_and_format(self):
        for data in (b"", b"not a frame", struct.pack("<III", 0, 900, 1),
                     struct.pack("<III", 1, 1, 999), struct.pack("<III", 0xFFFFFFFF, 900, 1)):
            with self.subTest(data=data), self.assertRaises(CaptureDecodeError):
                decode_raw_screencap(data)

    def test_reject_truncated_payload_and_shell_newline_corruption(self):
        data = raw_frame(np.array([[[13, 10, 99, 255]]], dtype=np.uint8), colorspace=1)
        for broken in (data[:-1], data + b"extra", data.replace(b"\n", b"\r\n")):
            with self.subTest(size=len(broken)), self.assertRaisesRegex(CaptureDecodeError, "size mismatch"):
                decode_raw_screencap(broken)

    def test_png_roundtrip_and_reject_other_formats(self):
        bgr = np.array([[[13, 10, 200], [200, 30, 55]]], dtype=np.uint8)
        encoded = AdbTransport("test").capture_png(bgr)
        np.testing.assert_array_equal(decode_png(encoded), bgr)
        for broken in (b"", b"jpeg", b"\x89PNG\r\n\x1a\n"):
            with self.subTest(data=broken), self.assertRaises(CaptureDecodeError):
                decode_png(broken)


class TransportTests(unittest.TestCase):
    def test_explicit_device_selection_and_raw_capture_command(self):
        rgba = np.array([[[1, 2, 3, 255]]], dtype=np.uint8)
        result = subprocess.CompletedProcess([], 0, raw_frame(rgba, colorspace=1), b"")
        transport = AdbTransport("127.0.0.1:5557", "test-adb", capture_timeout=2)
        with patch("pc_bot.adb_transport.subprocess.run", return_value=result) as run:
            np.testing.assert_array_equal(transport.capture(), [[[3, 2, 1]]])
        run.assert_called_once_with(["test-adb", "-s", "127.0.0.1:5557", "exec-out", "screencap"],
                                    capture_output=True, timeout=2)

    def test_png_mode_is_explicit(self):
        frame = np.array([[[4, 5, 6]]], dtype=np.uint8)
        png = AdbTransport("test").capture_png(frame)
        with patch("pc_bot.adb_transport.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 0, png, b"")) as run:
            np.testing.assert_array_equal(AdbTransport("test", capture_format="png").capture(), frame)
        self.assertEqual(run.call_args.args[0][-3:], ["exec-out", "screencap", "-p"])

    def test_malformed_capture_does_not_retry_or_fallback(self):
        with patch("pc_bot.adb_transport.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 0, b"invalid", b"")) as run:
            with self.assertRaises(CaptureDecodeError):
                AdbTransport("test").capture()
        self.assertEqual(run.call_count, 1)

    def test_disconnected_or_different_state_is_not_connected(self):
        for rc, state in ((0, b"device\n"), (0, b"offline\n"), (1, b"device\n")):
            with self.subTest(rc=rc, state=state), patch("pc_bot.adb_transport.subprocess.run", return_value=
                    subprocess.CompletedProcess([], rc, state, b"device offline")):
                self.assertEqual(AdbTransport("test").is_connected(), rc == 0 and state.strip() == b"device")

    def test_command_error_timeout_and_missing_executable_are_precise(self):
        for side_effect, message in ((FileNotFoundError(), "executable not found"),
                                     (subprocess.TimeoutExpired("adb", 2), "timed out")):
            with self.subTest(message=message), patch("pc_bot.adb_transport.subprocess.run", side_effect=side_effect):
                with self.assertRaisesRegex(AdbTransportError, message):
                    AdbTransport("test").capture()
        with patch("pc_bot.adb_transport.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 1, b"", b"device offline")):
            with self.assertRaisesRegex(AdbTransportError, "failed .*device offline"):
                AdbTransport("test").capture()

    def test_tap_uses_numeric_arguments_and_bounds_without_real_click(self):
        transport = AdbTransport("test", tap_timeout=3)
        transport._frame_size = (1600, 900)
        with patch("pc_bot.adb_transport.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 0, b"", b"")) as run:
            transport.tap(1330, 830)
            run.assert_called_once_with(["adb", "-s", "test", "shell", "input", "tap", "1330", "830"],
                                        capture_output=True, timeout=3)
        for x, y in ((-1, 0), (0, -1), (1600, 0), (0, 900), (float("nan"), 0)):
            with self.subTest(x=x, y=y), self.assertRaises(ValueError):
                transport.tap(x, y)

    def test_png_evidence_uses_existing_frame_without_second_adb_capture(self):
        with patch("pc_bot.adb_transport.subprocess.run") as run:
            transport = AdbTransport("test")
            png = transport.capture_png(np.zeros((2, 3, 3), dtype=np.uint8))
            self.assertEqual(decode_png(png).shape, (2, 3, 3))
        run.assert_not_called()

    def test_close_blocks_future_operations_without_killing_adb_server(self):
        with patch("pc_bot.adb_transport.subprocess.run") as run:
            transport = AdbTransport("test")
            transport.close()
            transport.close()
            for operation in (transport.capture, transport.is_connected, lambda: transport.tap(0, 0),
                              lambda: transport.capture_png(np.zeros((1, 1, 3), dtype=np.uint8))):
                with self.assertRaisesRegex(AdbTransportError, "closed"):
                    operation()
        run.assert_not_called()

    def test_configuration_validation(self):
        for options in ({"serial": ""}, {"serial": "test", "capture_format": "auto"},
                        {"serial": "test", "capture_timeout": 0}, {"serial": "test", "tap_timeout": float("inf")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                AdbTransport(**options)


if __name__ == "__main__":
    unittest.main()
