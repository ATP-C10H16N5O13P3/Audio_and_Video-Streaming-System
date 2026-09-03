# Audio and Video Streaming System
**V4L2 Raw MJPEG Capture & Hardware-Synced Encoder**

### Purpose

A high-precision, low-overhead Linux media capture, audio routing, and synchronization suite.
Made for RLdragon or the Research and Theorize Mythology Club

---

### Key Features

* **Hardware Timestamps:** Physical V4L2 driver timestamps directly from `v4l2_buffer` eliminate user-space jitter.
* **Drift-Free VFR Encoding:** Generates standards-compliant MP4s with microsecond-accurate Matroska V2 timecodes.
* **Zero-Loss Processing:** Direct in-memory MJPEG extraction (`0xFFD8`/`0xFFD9`) piped into FFmpeg without disk spam.
* **Virtual Audio Routing:** Routes OBS virtual monitor audio to target apps with selectable local headphone monitoring.
* **Wayland Delayed Monitor:** Borderless, adjustable real-time delayed playback window via PyQt5.

---

### Directory Structure

```text
.
├── config/
│   ├── audio.config          # Audio routing & virtual sink settings
│   └── video.config          # Camera, monitor & encoder settings
├── audio_bridge.py           # PulseAudio/PipeWire router
├── capture_monitor.py        # Real-time capture & delayed playback tool
├── encode.py                 # Precision V4L2 MJPEG VFR encoder
└── recordings/               # Output directory for video sessions
    └── capture_YYYYMMDD_HHMMSS/
        ├── video.mjpeg       # Raw bitstream
        ├── timestamps.txt    # Kernel hardware timestamps
        └── output_vfr.mp4    # Synchronized output video

```

---

### Prerequisites & Dependencies

#### 1. System Packages

```bash
# Debian / Ubuntu / Pop!_OS
sudo apt update && sudo apt install -y python3 python3-pip ffmpeg mkvtoolnix libv4l-dev pulseaudio-utils libportaudio2

# Arch Linux
sudo pacman -S python python-pip ffmpeg mkvtoolnix-cli libpulse portaudio

# Fedora
sudo dnf install -y python3 python3-pip ffmpeg mkvtoolnix pulseaudio-utils portaudio

```

#### 2. Python Packages & Permissions

```bash
pip install sounddevice numpy opencv-python PyQt5
sudo usermod -a -G video $USER  # Log out and back in to apply

```

---

### Configuration

All settings can be customized in the respective configuration files:

* **Video Settings:** Edit `config/video.config` to change camera device parameters, delay windows, FPS, and monitor preferences.
* **Audio Settings:** Edit `config/audio.config` to change sample rates, block sizes, and virtual PulseAudio/PipeWire sink names.

---

### Usage Guide

#### 1. Audio Routing

```bash
python3 audio_bridge.py

```

* **OBS Setup:** Set **Settings > Audio > Advanced > Monitoring Device** to `OBS_Input`.
* **Target App (Discord/Vesktop):** Set **Input Device** to `Monitor of Audio_Stream_Output`.
* **Monitor Selection:** Enter your headphone's numeric ID, or `none` to mirror the default sink.
* **Exit:** `q` / `Ctrl+C` preserves `OBS_Input` (prevents OBS device resets); `qc` unloads all virtual sinks.

#### 2. Video Capture & Delayed Monitor

```bash
python3 capture_monitor.py

```

| Key | Action |
| --- | --- |
| `+` / `=` | Increase playback delay (+0.5s) |
| `-` / `_` | Decrease playback delay (-0.5s) |
| `B` | Toggle window borders |
| `F` | Toggle fullscreen |
| `Q` / `Esc` | Stop capture and exit cleanly |

#### 3. Video Encoding (`encode.py`)

Converts raw camera dumps (`video.mjpeg` + `timestamps.txt`) into a drift-free VFR MP4:

```bash
python3 encode.py recordings/capture_YYYYMMDD_HHMMSS

```

The output file will be generated at `recordings/capture_YYYYMMDD_HHMMSS/output_vfr.mp4`.