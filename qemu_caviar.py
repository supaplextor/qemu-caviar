#!/usr/bin/env python3
"""
qemu-caviar: QEMU/KVM launcher with screenshot and video recording support.

Usage:
    qemu-caviar [--vm-name NAME] [--output-dir DIR] [-- QEMU_ARGS...]

Options:
    --vm-name NAME      Label shown in the window title and output filenames.
    --output-dir DIR    Directory for screenshots / recordings (default: $PWD).
    --                  Everything after this separator is forwarded verbatim
                        to qemu-system-x86_64.

Menu shortcuts (control window):
    Ctrl+S              Save screenshot (PNG)
    Ctrl+R              Toggle video recording (MP4)
    Ctrl+Q              Quit
"""

import argparse
import datetime
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402  (after require_version)

# ---------------------------------------------------------------------------
# QMP client
# ---------------------------------------------------------------------------


class QMPClient:
    """Minimal QEMU Machine Protocol (QMP) client over a Unix-domain socket."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._sock: socket.socket | None = None
        self._buf = b""

    # ------------------------------------------------------------------
    def connect(self, retries: int = 20, delay: float = 0.5) -> bool:
        """Try to connect, retrying up to *retries* times with *delay* seconds
        between attempts (QEMU may take a moment to create the socket)."""
        for attempt in range(retries):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.socket_path)
                self._sock = sock
                # Consume the greeting banner
                self._recv_object()
                # Negotiate capabilities (mandatory before any command)
                self._send({"execute": "qmp_capabilities"})
                self._recv_object()
                return True
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                sock.close()
                self._sock = None
                if attempt < retries - 1:
                    time.sleep(delay)
        return False

    # ------------------------------------------------------------------
    def _send(self, obj: dict) -> None:
        assert self._sock is not None
        data = (json.dumps(obj) + "\n").encode()
        self._sock.sendall(data)

    def _recv_object(self) -> dict:
        """Receive one complete JSON object from the socket."""
        assert self._sock is not None
        while True:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("QMP socket closed unexpectedly")
            self._buf += chunk
            # A QMP message always ends with a newline-terminated JSON object
            nl = self._buf.find(b"\n")
            if nl != -1:
                line, self._buf = self._buf[: nl + 1], self._buf[nl + 1 :]
                return json.loads(line)

    # ------------------------------------------------------------------
    def execute(self, command: str, **arguments) -> dict:
        payload: dict = {"execute": command}
        if arguments:
            payload["arguments"] = arguments
        self._send(payload)
        return self._recv_object()

    def screendump(self, filename: str) -> dict:
        """Ask QEMU to save the current framebuffer as a PNG file."""
        return self.execute("screendump", filename=filename)

    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _video_filename(output_dir: str) -> str:
    """Return an output path using the format qemu-video-YYYYMMDD-HHMMSS.NS.mp4.

    A single call to time.time_ns() is used as the sole time source so the
    date/time components and the nanosecond component are always consistent,
    even when the call straddles a second boundary.
    """
    ns_now = time.time_ns()
    dt = datetime.datetime.fromtimestamp(ns_now / 1e9)
    ns_part = ns_now % 1_000_000_000
    name = f"qemu-video-{dt.strftime('%Y%m%d-%H%M%S')}.{ns_part:09d}.mp4"
    return os.path.join(output_dir, name)


def _get_qemu_window_geometry(pid: int) -> tuple[int, int, int, int] | None:
    """Return (x, y, width, height) of the QEMU display window for *pid*.

    Uses ``xdotool`` to locate the visible window owned by *pid* and read its
    absolute position and size on the screen.  These coordinates can be passed
    directly to the ffmpeg x11grab input to restrict capture to just the QEMU
    window instead of the whole screen.

    Returns None if the window cannot be found or ``xdotool`` is unavailable
    (in which case the caller should fall back to full-screen capture).
    """
    try:
        win_ids_raw = subprocess.check_output(
            ["xdotool", "search", "--onlyvisible", "--pid", str(pid)],
            stderr=subprocess.DEVNULL,
            text=True,
        ).splitlines()
        win_ids = [w.strip() for w in win_ids_raw if w.strip()]
        if not win_ids:
            return None
        # QEMU may create more than one window; the last visible one is the
        # display window.
        win_id = win_ids[-1]
        geo = subprocess.check_output(
            ["xdotool", "getwindowgeometry", "--shell", win_id],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # Output format: X=…\nY=…\nWIDTH=…\nHEIGHT=…\nSCREEN=…\n
        vals: dict[str, int] = {}
        for line in geo.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                try:
                    vals[k.strip()] = int(v.strip())
                except ValueError:
                    pass
        if {"X", "Y", "WIDTH", "HEIGHT"}.issubset(vals):
            return vals["X"], vals["Y"], vals["WIDTH"], vals["HEIGHT"]
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    return None


def _find_vm_audio_source() -> str:
    """Return a PulseAudio source name that captures the VM's audio output.

    QEMU writes audio to PulseAudio.  To record what the VM is playing (rather
    than the host microphone) we use the *monitor* of the PulseAudio sink that
    QEMU is using.  A sink monitor captures all audio routed to that sink.

    Priority:
      1. A sink whose name contains "qemu" (QEMU-specific sink)
      2. The monitor of the system default sink
      3. Hard-coded fallback "default.monitor"
    """
    try:
        out = subprocess.check_output(
            ["pactl", "list", "sinks", "short"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and "qemu" in parts[1].lower():
                return parts[1] + ".monitor"
        # Fall back to the monitor of the system default sink
        default_sink = subprocess.check_output(
            ["pactl", "get-default-sink"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if default_sink:
            return default_sink + ".monitor"
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    return "default.monitor"


# How often (in seconds) the geometry watcher polls the QEMU window position
# and size while a recording is active.  Keeping this short enough to detect
# a guest-resolution change quickly without hammering xdotool.
_GEOMETRY_POLL_INTERVAL: float = 2.0


# ---------------------------------------------------------------------------
# Geometry watcher (restarts recordings when the QEMU window is resized)
# ---------------------------------------------------------------------------


def _run_geometry_watcher(
    stop: threading.Event,
    recorder: "Recorder",
    pid: int,
    display: str,
    output_dir: str,
    on_restarted: Callable[[str], None],
    on_failed: Callable[[], None],
) -> None:
    """Core geometry-watcher loop; intended to run in a background thread.

    Polls the QEMU window geometry every ``_GEOMETRY_POLL_INTERVAL``
    seconds.  When the window is resized or moved the current recording is
    stopped and a new one is started with the updated region so that the
    capture always matches the actual QEMU display area.

    The loop exits when *stop* is set, when *recorder.recording* becomes
    False, or when a restart attempt fails.

    Callbacks:
        on_restarted(new_path)  – called after a successful restart with the
                                   path of the new recording file.
        on_failed()             – called when a restart attempt fails.
    """
    current_region: tuple[int, int, int, int] | None = _get_qemu_window_geometry(pid)
    if current_region is None:
        # Cannot establish a baseline; bail out rather than triggering a
        # spurious restart the first time xdotool returns a valid geometry.
        return
    while not stop.wait(_GEOMETRY_POLL_INTERVAL):
        if not recorder.recording:
            break
        new_region = _get_qemu_window_geometry(pid)
        if new_region is None or new_region == current_region:
            continue
        # Geometry changed – stop the current recording and start a new one
        # with the updated region.  stop() may block briefly while ffmpeg
        # flushes its buffers; that is acceptable in a background thread.
        current_region = new_region
        recorder.stop()
        if stop.is_set():
            # The user requested a stop while we were restarting; don't resume.
            break
        new_outfile = _video_filename(output_dir)
        ok = recorder.start(new_outfile, display, region=new_region)
        if ok:
            on_restarted(new_outfile)
        else:
            on_failed()
            break


# ---------------------------------------------------------------------------
# Video / audio recorder
# ---------------------------------------------------------------------------


class Recorder:
    """Drives ffmpeg to capture the QEMU display (x11grab) and audio (pulse)
    and mux them into an MP4 file."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._outfile: str = ""
        self.recording: bool = False

    # ------------------------------------------------------------------
    def start(
        self,
        output_path: str,
        display: str = ":0",
        audio_source: str | None = None,
        region: tuple[int, int, int, int] | None = None,
    ) -> bool:
        """Start recording to *output_path*.  Returns True on success.

        *audio_source* is a PulseAudio source name (e.g. a sink monitor).
        If None, :func:`_find_vm_audio_source` is called automatically so
        that the recorded audio comes from the VM rather than the host mic.

        *region* is an optional ``(x, y, width, height)`` tuple that restricts
        the x11grab capture to a sub-region of *display*.  Pass the geometry
        of the QEMU window (obtained via :func:`_get_qemu_window_geometry`) to
        record only the VM display instead of the whole screen.  When *region*
        is None the entire display is captured.
        """
        if self.recording:
            print("[recorder] already recording", file=sys.stderr)
            return False

        if audio_source is None:
            audio_source = _find_vm_audio_source()

        # Build the x11grab video input arguments.  When a region is given,
        # restrict capture to that sub-rectangle of the display so that only
        # the QEMU window is recorded.  libx264 requires even dimensions, so
        # round width/height down to the nearest even number.
        if region is not None:
            x, y, w, h = region
            w = w - (w % 2)
            h = h - (h % 2)
            video_input = ["-video_size", f"{w}x{h}", "-i", f"{display}+{x},{y}"]
        else:
            video_input = ["-i", display]

        cmd = [
            "ffmpeg",
            "-y",
            # Video: grab the QEMU window area (or the whole display as fallback)
            "-f", "x11grab",
            "-framerate", "30",
            *video_input,
            # Audio: monitor of the PulseAudio sink that QEMU writes to,
            # so we capture what the VM is playing rather than the host mic.
            "-f", "pulse",
            "-i", audio_source,
            # Encode video with libx264 (fast preset) and audio with AAC.
            # yuv420p is required for broad player compatibility; x11grab
            # produces bgr0/bgra frames that libx264 would otherwise encode
            # in a non-standard pixel format causing garbled colour output.
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            output_path,
        ]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            _show_error("ffmpeg not found", "Please install ffmpeg to enable recording.")
            return False

        self._outfile = output_path
        self.recording = True
        return True

    # ------------------------------------------------------------------
    def stop(self) -> str | None:
        """Stop an active recording.  Returns the output file path."""
        if not self.recording or self._proc is None:
            return None

        # Ask ffmpeg to finish gracefully by writing 'q' to its stdin
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(b"q")
            self._proc.stdin.flush()
            self._proc.wait(timeout=15)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            self._proc.kill()
            self._proc.wait()

        self.recording = False
        outfile = self._outfile
        self._proc = None
        self._outfile = ""
        return outfile

    # ------------------------------------------------------------------
    def toggle(
        self,
        output_path: str,
        display: str = ":0",
        audio_source: str | None = None,
        region: tuple[int, int, int, int] | None = None,
    ) -> bool | None:
        """Toggle recording on/off.  Returns True when started, False when
        stopped, None on error."""
        if self.recording:
            result = self.stop()
            return False if result is not None else None
        else:
            return self.start(output_path, display, audio_source, region)


# ---------------------------------------------------------------------------
# GTK3 control window
# ---------------------------------------------------------------------------


def _show_error(title: str, message: str) -> None:
    """Display a simple GTK error dialog (safe to call from any thread via
    GLib.idle_add)."""

    def _dialog() -> bool:
        dlg = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dlg.format_secondary_text(message)
        dlg.run()
        dlg.destroy()
        return False  # don't repeat

    GLib.idle_add(_dialog)


def _show_info(title: str, message: str) -> None:
    def _dialog() -> bool:
        dlg = Gtk.MessageDialog(
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dlg.format_secondary_text(message)
        dlg.run()
        dlg.destroy()
        return False

    GLib.idle_add(_dialog)


class ControlWindow(Gtk.ApplicationWindow):
    """Small control window with a menu bar for screenshot / recording."""

    def __init__(
        self,
        app: "QemuCaviarApp",
        vm_name: str,
        output_dir: str,
        qmp: QMPClient | None,
    ) -> None:
        super().__init__(application=app, title=f"qemu-caviar – {vm_name}")
        self._vm_name = vm_name
        self._output_dir = output_dir
        self._qmp = qmp
        self._recorder = Recorder()
        # PID of the QEMU process; set by QemuCaviarApp once QEMU has started.
        # Used to look up the QEMU window geometry for window-scoped recording.
        self._qemu_pid: int | None = None
        # Set to a threading.Event when the geometry watcher is running; the
        # event is used to signal the watcher thread to exit cleanly.
        self._geometry_watcher_stop: threading.Event | None = None

        self.set_default_size(320, 80)
        self.set_resizable(False)
        self.connect("delete-event", self._on_delete)

        # ---- menu bar ----
        menubar = Gtk.MenuBar()

        # File menu
        file_menu_item = Gtk.MenuItem(label="_File")
        file_menu_item.set_use_underline(True)
        file_submenu = Gtk.Menu()
        file_menu_item.set_submenu(file_submenu)

        screenshot_item = Gtk.MenuItem(label="_Save Screenshot")
        screenshot_item.set_use_underline(True)
        screenshot_item.set_tooltip_text("Save a PNG screenshot (Ctrl+S)")
        screenshot_item.connect("activate", self._on_screenshot)
        file_submenu.append(screenshot_item)

        file_submenu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="_Quit")
        quit_item.set_use_underline(True)
        quit_item.set_tooltip_text("Quit qemu-caviar and stop QEMU (Ctrl+Q)")
        quit_item.connect("activate", lambda _: app.quit_all())
        file_submenu.append(quit_item)

        menubar.append(file_menu_item)

        # Recording menu
        rec_menu_item = Gtk.MenuItem(label="_Recording")
        rec_menu_item.set_use_underline(True)
        rec_submenu = Gtk.Menu()
        rec_menu_item.set_submenu(rec_submenu)

        self._rec_toggle_item = Gtk.MenuItem(label="_Start Recording")
        self._rec_toggle_item.set_use_underline(True)
        self._rec_toggle_item.set_tooltip_text(
            "Start / stop MP4 video+audio recording (Ctrl+R)"
        )
        self._rec_toggle_item.connect("activate", self._on_toggle_recording)
        rec_submenu.append(self._rec_toggle_item)

        menubar.append(rec_menu_item)

        # ---- keyboard shortcuts ----
        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        screenshot_item.add_accelerator(
            "activate", accel, ord("s"), GLib.PRIORITY_DEFAULT, Gtk.AccelFlags.VISIBLE
        )
        self._rec_toggle_item.add_accelerator(
            "activate", accel, ord("r"), GLib.PRIORITY_DEFAULT, Gtk.AccelFlags.VISIBLE
        )
        quit_item.add_accelerator(
            "activate", accel, ord("q"), GLib.PRIORITY_DEFAULT, Gtk.AccelFlags.VISIBLE
        )

        # ---- status label ----
        self._status_label = Gtk.Label(label="Ready")
        self._status_label.set_margin_start(8)
        self._status_label.set_margin_end(8)
        self._status_label.set_margin_top(4)
        self._status_label.set_margin_bottom(6)
        self._status_label.set_xalign(0)

        # ---- layout ----
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        vbox.pack_start(menubar, False, False, 0)
        vbox.pack_start(self._status_label, True, True, 0)
        self.add(vbox)

    # ------------------------------------------------------------------
    def _set_status(self, text: str) -> None:
        """Update the status label (safe from any thread via idle_add)."""
        GLib.idle_add(self._status_label.set_text, text)

    # ------------------------------------------------------------------
    def set_qemu_pid(self, pid: int) -> None:
        """Set the PID of the QEMU process for window-geometry lookup."""
        self._qemu_pid = pid

    # ------------------------------------------------------------------
    def _on_screenshot(self, _widget) -> None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(
            self._output_dir, f"{self._vm_name}-screenshot-{timestamp}.png"
        )
        if self._qmp is None or self._qmp._sock is None:
            _show_error(
                "No QEMU monitor",
                "Cannot take a screenshot: not connected to the QEMU monitor.\n"
                "Make sure QEMU was started with a -qmp socket.",
            )
            return
        try:
            result = self._qmp.screendump(filename)
        except OSError as exc:
            _show_error("Screenshot failed", str(exc))
            return
        if "error" in result:
            _show_error(
                "Screenshot failed",
                result["error"].get("desc", str(result["error"])),
            )
        else:
            self._set_status(f"Screenshot saved: {os.path.basename(filename)}")
            _show_info("Screenshot saved", filename)

    # ------------------------------------------------------------------
    def _on_toggle_recording(self, _widget) -> None:
        if self._recorder.recording:
            # Signal the geometry watcher (if running) to exit before we stop
            # the recorder, so it cannot race with us and restart the recording.
            if self._geometry_watcher_stop is not None:
                self._geometry_watcher_stop.set()
                self._geometry_watcher_stop = None
            # Stop
            def _stop_in_thread():
                outfile = self._recorder.stop()
                if outfile:
                    self._set_status(f"Recording saved: {os.path.basename(outfile)}")
                    _show_info("Recording saved", outfile)
                else:
                    self._set_status("Recording stopped (no output)")
                GLib.idle_add(
                    self._rec_toggle_item.set_label, "_Start Recording"
                )

            threading.Thread(target=_stop_in_thread, daemon=True).start()
            self._set_status("Stopping recording…")
        else:
            # Start – restrict capture to the QEMU window when its geometry
            # is available; fall back to the full display otherwise.
            outfile = _video_filename(self._output_dir)
            display = os.environ.get("DISPLAY", ":0")
            region: tuple[int, int, int, int] | None = None
            if self._qemu_pid is not None:
                region = _get_qemu_window_geometry(self._qemu_pid)
            ok = self._recorder.start(outfile, display, region=region)
            if ok:
                self._set_status(f"Recording… → {os.path.basename(outfile)}")
                self._rec_toggle_item.set_label("_Stop Recording")
                # When recording a specific window region, watch for QEMU
                # resizes so the capture region can be updated automatically.
                if region is not None and self._qemu_pid is not None:
                    self._geometry_watcher_stop = threading.Event()
                    threading.Thread(
                        target=self._geometry_watcher_loop,
                        args=(display,),
                        daemon=True,
                    ).start()

    # ------------------------------------------------------------------
    def _geometry_watcher_loop(self, display: str) -> None:
        """Delegate to :func:`_run_geometry_watcher` with this window's state."""
        stop = self._geometry_watcher_stop
        pid = self._qemu_pid
        if stop is None or pid is None:
            return

        def _on_restarted(path: str) -> None:
            GLib.idle_add(
                self._set_status,
                f"Recording… → {os.path.basename(path)}",
            )

        def _on_failed() -> None:
            GLib.idle_add(self._set_status, "Recording failed after resize")
            GLib.idle_add(self._rec_toggle_item.set_label, "_Start Recording")

        _run_geometry_watcher(
            stop=stop,
            recorder=self._recorder,
            pid=pid,
            display=display,
            output_dir=self._output_dir,
            on_restarted=_on_restarted,
            on_failed=_on_failed,
        )

    # ------------------------------------------------------------------
    def _on_delete(self, _widget, _event) -> bool:
        if self._geometry_watcher_stop is not None:
            self._geometry_watcher_stop.set()
            self._geometry_watcher_stop = None
        if self._recorder.recording:
            self._recorder.stop()
        return False  # allow window to close


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class QemuCaviarApp(Gtk.Application):
    """Main Gtk.Application that owns the QEMU process and the control window."""

    def __init__(
        self,
        vm_name: str,
        output_dir: str,
        qemu_args: list[str],
    ) -> None:
        super().__init__(application_id="io.github.supaplextor.qemu-caviar")
        self._vm_name = vm_name
        self._output_dir = output_dir
        self._qemu_args = qemu_args
        self._qemu_proc: subprocess.Popen | None = None
        self._qmp: QMPClient | None = None
        self._qmp_socket_path: str = ""
        self._win: ControlWindow | None = None

    # ------------------------------------------------------------------
    def do_activate(self) -> None:
        if self._win:
            self._win.present()
            return

        # Start QEMU in a background thread so the GTK main loop isn't blocked
        threading.Thread(target=self._start_qemu, daemon=True).start()

        self._win = ControlWindow(
            app=self,
            vm_name=self._vm_name,
            output_dir=self._output_dir,
            qmp=None,  # will be set once QMP connects
        )
        self._win.show_all()
        self._place_control_window()

    # ------------------------------------------------------------------
    def _place_control_window(self) -> None:
        """Move the control window to the top-right corner of the primary
        monitor so it does not overlap with the QEMU display window (which
        typically opens in the centre of the screen)."""
        if self._win is None:
            return
        screen = self._win.get_screen()
        monitor_idx = screen.get_primary_monitor()
        geo = screen.get_monitor_geometry(monitor_idx)
        win_w, _ = self._win.get_size()
        x = geo.x + geo.width - win_w - 8
        y = geo.y + 8
        self._win.move(x, y)

    # ------------------------------------------------------------------
    def _start_qemu(self) -> None:
        """Launch QEMU/KVM and connect the QMP client (runs in a thread)."""
        tmpdir = tempfile.mkdtemp(prefix="qemu-caviar-")
        self._qmp_socket_path = os.path.join(tmpdir, "qmp.sock")

        cmd = (
            ["qemu-system-x86_64"]
            + self._qemu_args
            + [
                "-enable-kvm",
                "-qmp", f"unix:{self._qmp_socket_path},server,nowait",
            ]
        )

        try:
            self._qemu_proc = subprocess.Popen(cmd)
        except FileNotFoundError:
            GLib.idle_add(
                lambda: _show_error(
                    "qemu-system-x86_64 not found",
                    "Please install QEMU: sudo apt install qemu-system-x86",
                )
            )
            return

        # Share the QEMU PID with the control window so it can find the
        # QEMU display window for window-scoped video recording.
        if self._win:
            GLib.idle_add(self._win.set_qemu_pid, self._qemu_proc.pid)

        # Connect QMP
        qmp = QMPClient(self._qmp_socket_path)
        connected = qmp.connect(retries=30, delay=0.5)
        if not connected:
            GLib.idle_add(
                lambda: _show_error(
                    "QMP connection failed",
                    "Could not connect to the QEMU monitor socket.\n"
                    "Screenshots will not be available.",
                )
            )
            return

        self._qmp = qmp
        # Inject the connected QMP into the control window
        if self._win:
            GLib.idle_add(setattr, self._win, "_qmp", qmp)

        # Watch for QEMU to exit and quit the application when it does
        self._qemu_proc.wait()
        GLib.idle_add(self.quit_all)

    # ------------------------------------------------------------------
    def quit_all(self) -> None:
        """Stop QEMU (if running) and quit the GTK application."""
        if self._qmp:
            try:
                self._qmp.execute("quit")
            except OSError:
                pass
            self._qmp.close()
            self._qmp = None
        if self._qemu_proc and self._qemu_proc.poll() is None:
            self._qemu_proc.terminate()
            try:
                self._qemu_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._qemu_proc.kill()
        self.quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qemu-caviar",
        description=(
            "QEMU/KVM launcher with a GTK control window offering "
            "screenshot (PNG) and video recording (MP4) menu options."
        ),
        epilog=(
            "All arguments after '--' are forwarded verbatim to "
            "qemu-system-x86_64."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--vm-name",
        default="qemu-vm",
        metavar="NAME",
        help="Label for window title and output filenames (default: qemu-vm)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path.cwd()),
        metavar="DIR",
        help="Directory for screenshots and recordings (default: $PWD)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Split our args from QEMU args at '--'
    if "--" in argv:
        sep = argv.index("--")
        our_argv, qemu_args = argv[:sep], argv[sep + 1 :]
    else:
        our_argv, qemu_args = argv, []

    parser = _build_arg_parser()
    args = parser.parse_args(our_argv)

    output_dir = os.path.expanduser(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    app = QemuCaviarApp(
        vm_name=args.vm_name,
        output_dir=output_dir,
        qemu_args=qemu_args,
    )

    # Forward SIGTERM / SIGINT to the GTK quit path
    signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(app.quit_all))
    signal.signal(signal.SIGINT, lambda *_: GLib.idle_add(app.quit_all))

    return app.run([])


if __name__ == "__main__":
    sys.exit(main())
