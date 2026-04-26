# qemu-caviar

QEMU/KVM launcher with a **GTK control window** that provides:

- 📸 **File → Save Screenshot** – saves the current QEMU framebuffer as a timestamped PNG via the QEMU Machine Protocol (QMP).
- 🎬 **Recording → Start/Stop Recording** – drives `ffmpeg` to capture the display + audio and mux the result into an MP4 file.

## Requirements

| Package | Purpose |
|---------|---------|
| `qemu-system-x86` | QEMU/KVM hypervisor |
| `python3-gi` + `gir1.2-gtk-3.0` | GTK3 Python bindings |
| `ffmpeg` | Video/audio recording (optional) |

Install on Ubuntu/Debian:

```bash
sudo apt install qemu-system-x86 python3-gi gir1.2-gtk-3.0 ffmpeg
```

## Usage

```
qemu-caviar [--vm-name NAME] [--output-dir DIR] [-- QEMU_ARGS...]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--vm-name NAME` | `qemu-vm` | Label used in the window title and output filenames |
| `--output-dir DIR` | `$PWD` | Directory where screenshots and recordings are saved |
| `--` | – | Everything after this separator is forwarded to `qemu-system-x86_64` |

### Examples

```bash
# Launch a VM from an ISO image
python3 qemu_caviar.py --vm-name myvm -- -m 2G -cdrom ubuntu.iso

# Launch with a disk image and 4 GB RAM
python3 qemu_caviar.py --vm-name myvm --output-dir ~/captures \
    -- -m 4G -hda myvm.qcow2
```

## Control window

A small floating GTK window opens alongside the QEMU display:

```
┌─────────────────────────────────────────┐
│ File   Recording                        │
├─────────────────────────────────────────┤
│ Ready                                   │
└─────────────────────────────────────────┘
```

| Menu item | Shortcut | Action |
|-----------|----------|--------|
| File → Save Screenshot | Ctrl+S | Saves `<vm-name>-screenshot-<timestamp>.png` |
| Recording → Start/Stop Recording | Ctrl+R | Toggles MP4 recording; saves `<vm-name>-recording-<timestamp>.mp4` |
| File → Quit | Ctrl+Q | Stops recording (if active), terminates QEMU, exits |

## How it works

1. **QMP** – QEMU is started with `-qmp unix:<socket>,server,nowait`. `qemu-caviar` connects to that socket and issues a `screendump` command when you choose *Save Screenshot*.
2. **Recording** – `ffmpeg` is invoked with `-f x11grab` (X11 display capture) and `-f pulse` pointed at the PulseAudio *monitor* of the sink QEMU writes to, capturing the VM's audio output rather than the host microphone. The streams are encoded as H.264 video + AAC audio inside an MP4 container.

## Output files

| File | Description |
|------|-------------|
| `<vm-name>-screenshot-<YYYYMMDD_HHMMSS>.png` | PNG framebuffer dump |
| `qemu-video-<YYYYMMDD>-<HHMMSS>.<NS>.mp4` | MP4 video recording with VM audio (NS = nanoseconds) |
