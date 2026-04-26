#!/usr/bin/env python3
"""
qemu-caviar: QEMU/KVM launcher with screenshot and video recording support.

Usage:
    qemu-caviar [--vm-name NAME] [--output-dir DIR] [-- QEMU_ARGS...]

Options:
    --vm-name NAME      Label shown in the window title and output filenames.
    --output-dir DIR    Directory for screenshots / recordings (default: $HOME).
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
    def start(self, output_path: str, display: str = ":0", audio_source: str | None = None) -> bool:
        """Start recording to *output_path*.  Returns True on success.

        *audio_source* is a PulseAudio source name (e.g. a sink monitor).
        If None, :func:`_find_vm_audio_source` is called automatically so
        that the recorded audio comes from the VM rather than the host mic.
        """
        if self.recording:
            print("[recorder] already recording", file=sys.stderr)
            return False

        if audio_source is None:
            audio_source = _find_vm_audio_source()

        cmd = [
            "ffmpeg",
            "-y",
            # Video: grab the whole X11 display
            "-f", "x11grab",
            "-framerate", "30",
            "-i", display,
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
    def toggle(self, output_path: str, display: str = ":0", audio_source: str | None = None) -> bool | None:
        """Toggle recording on/off.  Returns True when started, False when
        stopped, None on error."""
        if self.recording:
            result = self.stop()
            return False if result is not None else None
        else:
            return self.start(output_path, display, audio_source)


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
            # Start
            outfile = _video_filename(self._output_dir)
            display = os.environ.get("DISPLAY", ":0")
            ok = self._recorder.start(outfile, display)
            if ok:
                self._set_status(f"Recording… → {os.path.basename(outfile)}")
                self._rec_toggle_item.set_label("_Stop Recording")

    # ------------------------------------------------------------------
    def _on_delete(self, _widget, _event) -> bool:
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
        default=str(Path.home()),
        metavar="DIR",
        help="Directory for screenshots and recordings (default: $HOME)",
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
