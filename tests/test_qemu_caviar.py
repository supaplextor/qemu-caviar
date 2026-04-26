"""Unit tests for qemu_caviar.py – covers QMPClient, Recorder, and arg
parsing without requiring a running QEMU process, a real display, or ffmpeg."""

import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure the module is importable from the repo root without installation
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import qemu_caviar  # noqa: E402


# ---------------------------------------------------------------------------
# QMPClient tests
# ---------------------------------------------------------------------------


class _QMPServer(threading.Thread):
    """Minimal fake QMP server used in tests."""

    def __init__(self, sock_path: str) -> None:
        super().__init__(daemon=True)
        self.sock_path = sock_path
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(sock_path)
        self._server.listen(1)
        self.received: list[dict] = []
        self._responses: list[bytes] = []
        self._ready = threading.Event()

    def queue_response(self, obj: dict) -> None:
        self._responses.append((json.dumps(obj) + "\n").encode())

    def run(self) -> None:
        self._ready.set()
        conn, _ = self._server.accept()
        # Send QMP greeting
        conn.sendall(
            (
                json.dumps({"QMP": {"version": {}, "capabilities": []}}) + "\n"
            ).encode()
        )
        buf = b""
        while True:
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                nl = buf.find(b"\n")
                if nl == -1:
                    continue
                line, buf = buf[: nl + 1], buf[nl + 1 :]
                obj = json.loads(line)
                self.received.append(obj)
                if self._responses:
                    conn.sendall(self._responses.pop(0))
                else:
                    conn.sendall((json.dumps({"return": {}}) + "\n").encode())
            except OSError:
                break
        conn.close()
        self._server.close()


class TestQMPClient(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._sock_path = os.path.join(self._tmpdir, "qmp.sock")
        self._server = _QMPServer(self._sock_path)
        self._server.start()
        self._server._ready.wait()

    def _make_client(self) -> qemu_caviar.QMPClient:
        client = qemu_caviar.QMPClient(self._sock_path)
        self.assertTrue(client.connect(retries=10, delay=0.1))
        return client

    def test_connect_success(self) -> None:
        client = self._make_client()
        self.assertIsNotNone(client._sock)
        client.close()

    def test_execute_screendump(self) -> None:
        client = self._make_client()
        tmpdir = tempfile.mkdtemp()
        png_path = os.path.join(tmpdir, "test.png")
        self._server.queue_response({"return": {}})
        result = client.screendump(png_path)
        self.assertIn("return", result)
        screendump_cmds = [
            c for c in self._server.received if c.get("execute") == "screendump"
        ]
        self.assertEqual(len(screendump_cmds), 1)
        self.assertEqual(
            screendump_cmds[0]["arguments"]["filename"], png_path
        )
        client.close()

    def test_connect_fails_no_socket(self) -> None:
        client = qemu_caviar.QMPClient("/nonexistent/path/qmp.sock")
        result = client.connect(retries=2, delay=0.05)
        self.assertFalse(result)

    def test_close_idempotent(self) -> None:
        client = self._make_client()
        client.close()
        client.close()  # must not raise


# ---------------------------------------------------------------------------
# _video_filename tests
# ---------------------------------------------------------------------------


class TestVideoFilename(unittest.TestCase):
    _PATTERN = re.compile(
        r"qemu-video-\d{8}-\d{6}\.\d{9}\.mp4$"
    )

    def test_filename_format(self) -> None:
        tmpdir = tempfile.mkdtemp()
        path = qemu_caviar._video_filename(tmpdir)
        self.assertEqual(os.path.dirname(path), tmpdir)
        self.assertRegex(os.path.basename(path), self._PATTERN)

    def test_unique_filenames(self) -> None:
        """All 10 rapid-fire calls must produce distinct filenames."""
        tmpdir = tempfile.mkdtemp()
        paths = [qemu_caviar._video_filename(tmpdir) for _ in range(10)]
        self.assertEqual(len(paths), len(set(paths)))


# ---------------------------------------------------------------------------
# _find_vm_audio_source tests
# ---------------------------------------------------------------------------


class TestFindVmAudioSource(unittest.TestCase):
    def test_prefers_qemu_sink(self) -> None:
        pactl_list = "0\tqemu-audio-sink\tRUNNING\n1\talsa_output.foo\tIDLE\n"
        with patch(
            "subprocess.check_output",
            side_effect=[pactl_list, "alsa_output.foo"],
        ):
            src = qemu_caviar._find_vm_audio_source()
        self.assertEqual(src, "qemu-audio-sink.monitor")

    def test_falls_back_to_default_sink(self) -> None:
        pactl_list = "0\talsa_output.pci.analog-stereo\tRUNNING\n"
        default_sink = "alsa_output.pci.analog-stereo"
        with patch(
            "subprocess.check_output",
            side_effect=[pactl_list, default_sink],
        ):
            src = qemu_caviar._find_vm_audio_source()
        self.assertEqual(src, "alsa_output.pci.analog-stereo.monitor")

    def test_fallback_when_pactl_missing(self) -> None:
        with patch(
            "subprocess.check_output",
            side_effect=FileNotFoundError("pactl"),
        ):
            src = qemu_caviar._find_vm_audio_source()
        self.assertEqual(src, "default.monitor")


# ---------------------------------------------------------------------------
# Recorder tests
# ---------------------------------------------------------------------------


class TestRecorder(unittest.TestCase):
    def _mock_proc(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.wait = MagicMock(return_value=0)
        return mock_proc

    def test_start_stop_with_mock_ffmpeg(self) -> None:
        recorder = qemu_caviar.Recorder()
        tmpdir = tempfile.mkdtemp()
        outpath = os.path.join(tmpdir, "test.mp4")
        with patch("subprocess.Popen", return_value=self._mock_proc()):
            with patch.object(
                qemu_caviar, "_find_vm_audio_source", return_value="default.monitor"
            ):
                ok = recorder.start(outpath, display=":99")

        self.assertTrue(ok)
        self.assertTrue(recorder.recording)
        self.assertEqual(recorder._outfile, outpath)

        outfile = recorder.stop()
        self.assertEqual(outfile, outpath)
        self.assertFalse(recorder.recording)

    def test_explicit_audio_source_passed_to_ffmpeg(self) -> None:
        """Verify the explicit audio_source overrides auto-detection."""
        recorder = qemu_caviar.Recorder()
        tmpdir = tempfile.mkdtemp()
        outpath = os.path.join(tmpdir, "test.mp4")
        captured_cmd: list[str] = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._mock_proc()

        with patch("subprocess.Popen", side_effect=fake_popen):
            recorder.start(outpath, display=":99", audio_source="my-qemu-sink.monitor")

        pulse_idx = captured_cmd.index("pulse") + 2  # "-f pulse -i <src>"
        self.assertEqual(captured_cmd[pulse_idx], "my-qemu-sink.monitor")
        recorder.stop()

    def test_pix_fmt_yuv420p_in_ffmpeg_cmd(self) -> None:
        """yuv420p must be set so x11grab frames are correctly encoded by
        libx264; omitting it causes garbled colour blocks in the output."""
        recorder = qemu_caviar.Recorder()
        tmpdir = tempfile.mkdtemp()
        outpath = os.path.join(tmpdir, "test.mp4")
        captured_cmd: list[str] = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._mock_proc()

        with patch("subprocess.Popen", side_effect=fake_popen):
            recorder.start(outpath, display=":99", audio_source="default.monitor")

        self.assertIn("-pix_fmt", captured_cmd)
        pix_fmt_idx = captured_cmd.index("-pix_fmt")
        self.assertEqual(captured_cmd[pix_fmt_idx + 1], "yuv420p")
        recorder.stop()

    def test_start_without_ffmpeg(self) -> None:
        recorder = qemu_caviar.Recorder()
        tmpdir = tempfile.mkdtemp()
        outpath = os.path.join(tmpdir, "test.mp4")
        with patch("subprocess.Popen", side_effect=FileNotFoundError("ffmpeg")):
            with patch.object(qemu_caviar, "_show_error"):
                with patch.object(
                    qemu_caviar, "_find_vm_audio_source", return_value="default.monitor"
                ):
                    ok = recorder.start(outpath)
        self.assertFalse(ok)
        self.assertFalse(recorder.recording)

    def test_double_start_rejected(self) -> None:
        recorder = qemu_caviar.Recorder()
        tmpdir = tempfile.mkdtemp()
        with patch("subprocess.Popen", return_value=self._mock_proc()):
            with patch.object(
                qemu_caviar, "_find_vm_audio_source", return_value="default.monitor"
            ):
                recorder.start(os.path.join(tmpdir, "a.mp4"))
                ok = recorder.start(os.path.join(tmpdir, "b.mp4"))
        self.assertFalse(ok)
        recorder.stop()

    def test_stop_when_not_recording(self) -> None:
        recorder = qemu_caviar.Recorder()
        result = recorder.stop()
        self.assertIsNone(result)

    def test_region_sets_video_size_and_offset_in_ffmpeg_cmd(self) -> None:
        """When a region is given, -video_size and the display+x,y offset must
        appear in the ffmpeg command so that only the QEMU window is recorded."""
        recorder = qemu_caviar.Recorder()
        tmpdir = tempfile.mkdtemp()
        outpath = os.path.join(tmpdir, "region.mp4")
        captured_cmd: list[str] = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._mock_proc()

        with patch("subprocess.Popen", side_effect=fake_popen):
            recorder.start(
                outpath,
                display=":99",
                audio_source="default.monitor",
                region=(100, 200, 1280, 720),
            )

        self.assertIn("-video_size", captured_cmd)
        size_idx = captured_cmd.index("-video_size")
        self.assertEqual(captured_cmd[size_idx + 1], "1280x720")

        # The -i argument must encode the offset into the display string
        self.assertIn(":99+100,200", captured_cmd)
        recorder.stop()

    def test_region_odd_dimensions_rounded_down(self) -> None:
        """libx264 requires even dimensions; odd width/height must be rounded
        down to the nearest even number before being passed to ffmpeg."""
        recorder = qemu_caviar.Recorder()
        tmpdir = tempfile.mkdtemp()
        outpath = os.path.join(tmpdir, "odd.mp4")
        captured_cmd: list[str] = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._mock_proc()

        with patch("subprocess.Popen", side_effect=fake_popen):
            recorder.start(
                outpath,
                display=":0",
                audio_source="default.monitor",
                region=(0, 0, 1281, 721),
            )

        size_idx = captured_cmd.index("-video_size")
        # 1281 → 1280, 721 → 720
        self.assertEqual(captured_cmd[size_idx + 1], "1280x720")
        recorder.stop()

    def test_no_region_no_video_size_flag(self) -> None:
        """When no region is given, -video_size must NOT appear in the command
        so that the full display is captured."""
        recorder = qemu_caviar.Recorder()
        tmpdir = tempfile.mkdtemp()
        outpath = os.path.join(tmpdir, "full.mp4")
        captured_cmd: list[str] = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._mock_proc()

        with patch("subprocess.Popen", side_effect=fake_popen):
            recorder.start(outpath, display=":0", audio_source="default.monitor")

        self.assertNotIn("-video_size", captured_cmd)
        # The display string must appear without an offset
        self.assertIn(":0", captured_cmd)
        recorder.stop()

    def test_toggle_forwards_region(self) -> None:
        """toggle() must pass the region through to start()."""
        recorder = qemu_caviar.Recorder()
        tmpdir = tempfile.mkdtemp()
        outpath = os.path.join(tmpdir, "toggle_region.mp4")
        captured_cmd: list[str] = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._mock_proc()

        with patch("subprocess.Popen", side_effect=fake_popen):
            with patch.object(
                qemu_caviar, "_find_vm_audio_source", return_value="default.monitor"
            ):
                recorder.toggle(outpath, display=":0", region=(10, 20, 800, 600))

        self.assertIn("-video_size", captured_cmd)
        size_idx = captured_cmd.index("-video_size")
        self.assertEqual(captured_cmd[size_idx + 1], "800x600")
        recorder.stop()


# ---------------------------------------------------------------------------
# _get_qemu_window_geometry tests
# ---------------------------------------------------------------------------


class TestGetQemuWindowGeometry(unittest.TestCase):
    def test_returns_geometry_when_xdotool_succeeds(self) -> None:
        xdotool_search_out = "123456\n"
        xdotool_geo_out = "X=100\nY=200\nWIDTH=1280\nHEIGHT=720\nSCREEN=0\n"
        with patch(
            "subprocess.check_output",
            side_effect=[xdotool_search_out, xdotool_geo_out],
        ):
            result = qemu_caviar._get_qemu_window_geometry(9999)
        self.assertEqual(result, (100, 200, 1280, 720))

    def test_returns_none_when_no_windows_found(self) -> None:
        with patch("subprocess.check_output", return_value=""):
            result = qemu_caviar._get_qemu_window_geometry(9999)
        self.assertIsNone(result)

    def test_returns_none_when_xdotool_missing(self) -> None:
        with patch(
            "subprocess.check_output",
            side_effect=FileNotFoundError("xdotool"),
        ):
            result = qemu_caviar._get_qemu_window_geometry(9999)
        self.assertIsNone(result)

    def test_returns_none_when_xdotool_fails(self) -> None:
        with patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "xdotool"),
        ):
            result = qemu_caviar._get_qemu_window_geometry(9999)
        self.assertIsNone(result)

    def test_uses_last_window_id_when_multiple_returned(self) -> None:
        """QEMU may create multiple windows; the last visible one is the display."""
        xdotool_search_out = "111\n222\n333\n"
        xdotool_geo_out = "X=50\nY=60\nWIDTH=640\nHEIGHT=480\nSCREEN=0\n"
        captured_calls: list = []

        def fake_check_output(cmd, **kwargs):
            captured_calls.append(list(cmd))
            if "search" in cmd:
                return xdotool_search_out
            return xdotool_geo_out

        with patch("subprocess.check_output", side_effect=fake_check_output):
            result = qemu_caviar._get_qemu_window_geometry(1234)

        # Second call (getwindowgeometry) must use the last ID ("333")
        geo_call = captured_calls[1]
        self.assertIn("333", geo_call)
        self.assertEqual(result, (50, 60, 640, 480))


# ---------------------------------------------------------------------------
# Argument parser tests
# ---------------------------------------------------------------------------


class TestArgParser(unittest.TestCase):
    def _parse(self, argv):
        if "--" in argv:
            sep = argv.index("--")
            our_argv, qemu_args = argv[:sep], argv[sep + 1:]
        else:
            our_argv, qemu_args = argv, []
        parser = qemu_caviar._build_arg_parser()
        args = parser.parse_args(our_argv)
        return args, qemu_args

    def test_defaults(self) -> None:
        args, qemu_args = self._parse([])
        self.assertEqual(args.vm_name, "qemu-vm")
        self.assertEqual(args.output_dir, str(os.path.expanduser("~")))
        self.assertEqual(qemu_args, [])

    def test_custom_vm_name(self) -> None:
        args, _ = self._parse(["--vm-name", "myserver"])
        self.assertEqual(args.vm_name, "myserver")

    def test_custom_output_dir(self) -> None:
        args, _ = self._parse(["--output-dir", "/home/user/captures"])
        self.assertEqual(args.output_dir, "/home/user/captures")

    def test_qemu_args_forwarded(self) -> None:
        _, qemu_args = self._parse(
            ["--vm-name", "test", "--", "-m", "2G", "-cdrom", "disk.iso"]
        )
        self.assertEqual(qemu_args, ["-m", "2G", "-cdrom", "disk.iso"])

    def test_separator_only(self) -> None:
        args, qemu_args = self._parse(["--", "-hda", "vm.qcow2"])
        self.assertEqual(args.vm_name, "qemu-vm")
        self.assertEqual(qemu_args, ["-hda", "vm.qcow2"])


if __name__ == "__main__":
    unittest.main()

