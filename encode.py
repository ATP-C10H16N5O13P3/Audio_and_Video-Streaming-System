#!/usr/bin/env python3
"""
Hardware Timestamp-Synchronized Video Encoder
- Reads config/video.config using configparser.
- Supports both CFR (recommended for Premiere/Resolve/FCP) and VFR modes.
- Parses raw MJPEG buffers byte-by-byte (zero intermediate JPEG disk overhead).
- Uses mkvmerge timecode v2 integration for sub-millisecond physical time locking.
"""

import configparser
import os
import shutil
import subprocess
import sys

CONFIG_PATH = os.path.join("config", "video.config")
SOI_MARKER = b"\xff\xd8"
EOI_MARKER = b"\xff\xd9"

DEFAULT_ENCODING_CONFIG = {
    "recording": {
        "output_base_dir": "recordings",
        "video_filename": "video.mjpeg",
        "timestamps_filename": "timestamps.txt",
    },
    "encoding": {
        "mode": "cfr",
        "target_fps": "60",
        "crf": "18",
        "preset": "fast",
        "pixel_format": "yuv420p",
        "output_filename": "output_synced.mp4",
    },
}


def load_config(path=CONFIG_PATH):
  config = configparser.ConfigParser()
  if os.path.exists(path):
    config.read(path)
  else:
    config.read_dict(DEFAULT_ENCODING_CONFIG)
  return config


def extract_raw_mjpeg_frames(mjpeg_path: str):
  """Scans the raw MJPEG file chunk-by-chunk for SOI/EOI markers and yields full JPEG frames."""
  buffer = bytearray()
  chunk_size = 1024 * 1024

  with open(mjpeg_path, "rb") as f:
    while True:
      chunk = f.read(chunk_size)
      if not chunk:
        break
      buffer.extend(chunk)

      while True:
        soi_idx = buffer.find(SOI_MARKER)
        if soi_idx == -1:
          buffer.clear()
          break

        eoi_idx = buffer.find(EOI_MARKER, soi_idx + 2)
        if eoi_idx == -1:
          if soi_idx > 0:
            del buffer[:soi_idx]
          break

        frame_data = bytes(buffer[soi_idx : eoi_idx + 2])
        del buffer[: eoi_idx + 2]
        yield frame_data


def main():
  if len(sys.argv) < 2:
    print("Usage: python3 encode_video.py <path_to_capture_folder>")
    sys.exit(1)

  config = load_config()

  # Load options from config
  target_dir = sys.argv[1].rstrip("/")
  video_fn = config.get(
      "recording", "video_filename", fallback="video.mjpeg"
  )
  ts_fn = config.get(
      "recording", "timestamps_filename", fallback="timestamps.txt"
  )

  encode_mode = config.get("encoding", "mode", fallback="cfr").strip().lower()
  target_fps = config.getint("encoding", "target_fps", fallback=60)
  crf = config.get("encoding", "crf", fallback="18")
  preset = config.get("encoding", "preset", fallback="fast")
  pix_fmt = config.get("encoding", "pixel_format", fallback="yuv420p")
  out_name = config.get(
      "encoding", "output_filename", fallback="output.mp4"
  )

  mjpeg_file = os.path.join(target_dir, video_fn)
  ts_file = os.path.join(target_dir, ts_fn)
  output_path = os.path.join(target_dir, out_name)

  timecodes_file = os.path.join(target_dir, "temp_timecodes.txt")
  temp_raw_mkv = os.path.join(target_dir, "temp_raw.mkv")
  timed_mkv = os.path.join(target_dir, "temp_timed.mkv")

  if not os.path.exists(mjpeg_file) or not os.path.exists(ts_file):
    print(
        f"[!] Error: Missing '{video_fn}' or '{ts_fn}' in directory:"
        f" '{target_dir}'"
    )
    sys.exit(1)

  if not shutil.which("mkvmerge"):
    print("[!] Error: 'mkvmerge' is required for bit-perfect hardware timing.")
    print("    Install it via: sudo apt install mkvtoolnix")
    sys.exit(1)

  # 1. Parse and zero-normalize hardware timestamps
  print("[*] Step 1/4: Parsing hardware timestamps...")
  raw_ts = []
  with open(ts_file, "r", encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if line:
        try:
          raw_ts.append(float(line))
        except ValueError:
          continue

  if not raw_ts:
    print("[!] Error: Timestamp file is empty.")
    sys.exit(1)

  t0 = raw_ts[0]
  norm_ts = []
  last = 0.0
  for ts in raw_ts:
    delta_ms = (ts - t0) * 1000.0
    if delta_ms < last:
      delta_ms = last + 1.0
    norm_ts.append(delta_ms)
    last = delta_ms

  duration_sec = (raw_ts[-1] - raw_ts[0])
  print(
      f"    -> Parsed {len(norm_ts)} timestamps | Total Duration:"
      f" {duration_sec:.3f}s"
  )

  # 2. Generate Matroska Timecode v2 definition file
  print("[*] Step 2/4: Generating Matroska V2 timecodes...")
  with open(timecodes_file, "w", encoding="utf-8") as f:
    f.write("# timecode format v2\n")
    for ms in norm_ts:
      f.write(f"{ms:.3f}\n")

  # 3. Stream frames into a temporary MKV container without re-encoding (Lossless)
  print("[*] Step 3/4: Demuxing raw MJPEG bitstream into container...")
  pipe_cmd = [
      "ffmpeg",
      "-y",
      "-f",
      "image2pipe",
      "-c:v",
      "mjpeg",
      "-i",
      "-",
      "-c:v",
      "copy",
      temp_raw_mkv,
  ]

  proc = subprocess.Popen(
      pipe_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
  )
  frames_written = 0
  for frame_data in extract_raw_mjpeg_frames(mjpeg_file):
    if frames_written >= len(norm_ts):
      break
    proc.stdin.write(frame_data)
    frames_written += 1
    if frames_written % 150 == 0:
      sys.stdout.write(f"\r    -> Ingested {frames_written} frames...")
      sys.stdout.flush()

  proc.stdin.close()
  proc.wait()
  print(f"\r    -> Successfully mapped {frames_written} frames.")

  # 4. Bind hardware timestamps into container
  print("[*] Step 4/4: Merging exact hardware PTS into container...")
  subprocess.run(
      [
          "mkvmerge",
          "-o",
          timed_mkv,
          "--timestamps",
          f"0:{timecodes_file}",
          temp_raw_mkv,
      ],
      stdout=subprocess.DEVNULL,
  )

  # 5. Encode to final MP4 according to mode
  print(f"[*] Finalizing output in mode [{encode_mode.upper()}]...")

  if encode_mode == "cfr":
    # CFR Mode: Uses fps filter to conform variable intervals to standard fixed fps
    final_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        timed_mkv,
        "-vf",
        f"fps={target_fps}:round=near",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        pix_fmt,
        output_path,
    ]
  else:
    # VFR Mode: Retains raw presentation timestamps directly
    final_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        timed_mkv,
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-fps_mode",
        "vfr",
        "-pix_fmt",
        pix_fmt,
        output_path,
    ]

  result = subprocess.run(final_cmd)

  # Cleanup temporary artifacts
  for path in (timecodes_file, temp_raw_mkv, timed_mkv):
    if os.path.exists(path):
      try:
        os.remove(path)
      except OSError:
        pass

  if result.returncode == 0 and os.path.exists(output_path):
    print(f"\n[+] Success! Synced video written to: {output_path}")
    print(f"    Wall-clock duration: {duration_sec:.3f} seconds.")
  else:
    print("\n[!] Encoding failed. Check FFmpeg error logs.")


if __name__ == "__main__":
  main()