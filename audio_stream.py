import os
import sys
import queue
import threading
import subprocess
import atexit
import time
import numpy as np
import sounddevice as sd

# Store loaded module IDs so we can target-unload them
OBS_MODULE_ID = None
OUTPUT_MODULE_ID = None
CLEANUP_ALL = False  # Set to True only when 'qc' is called


def run_cmd(cmd, check=True):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check)
    return result.stdout.strip()


def get_existing_sink_module_id(sink_name):
    """Find the module ID of an existing sink name to avoid rebuilding it."""
    try:
        output = run_cmd(["pactl", "list", "short", "modules"], check=False)
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[1] == "module-null-sink":
                if f"sink_name={sink_name}" in parts[2]:
                    return parts[0]
    except Exception:
        pass
    return None


def setup_pulse_sinks():
    global OBS_MODULE_ID, OUTPUT_MODULE_ID
    print("\n[PulseAudio] Configuring virtual sinks...")

    # Preserve OBS_Input if it already exists
    existing_obs_id = get_existing_sink_module_id("OBS_Input")
    if existing_obs_id:
        OBS_MODULE_ID = existing_obs_id
        print(f"[PulseAudio] 'OBS_Input' already exists (Module ID: {OBS_MODULE_ID}). Keeping it intact.")
    else:
        try:
            OBS_MODULE_ID = run_cmd([
                "pactl", "load-module", "module-null-sink",
                "sink_name=OBS_Input",
                'sink_properties=device.description="OBS_Input"'
            ])
            print(f"[PulseAudio] Created 'OBS_Input' (Module ID: {OBS_MODULE_ID})")
        except subprocess.CalledProcessError as e:
            print(f"[PulseAudio Error] Failed to create OBS_Input: {e.stderr}", file=sys.stderr)
            sys.exit(1)

    # Clean up old Audio_Stream_Output from previous runs
    old_output_id = get_existing_sink_module_id("Audio_Stream_Output")
    if old_output_id:
        subprocess.run(["pactl", "unload-module", old_output_id], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    try:
        OUTPUT_MODULE_ID = run_cmd([
            "pactl", "load-module", "module-null-sink",
            "sink_name=Audio_Stream_Output",
            'sink_properties=device.description="Audio_Stream_Output"'
        ])
        print(f"[PulseAudio] Created 'Audio_Stream_Output' (Module ID: {OUTPUT_MODULE_ID})")
    except subprocess.CalledProcessError as e:
        print(f"[PulseAudio Error] Failed to create Audio_Stream_Output: {e.stderr}", file=sys.stderr)
        sys.exit(1)


def cleanup_pulse_sinks():
    """Selectively unloads sinks based on whether a full clean was requested."""
    global OUTPUT_MODULE_ID, OBS_MODULE_ID

    print("\n[PulseAudio] Running teardown...")

    if OUTPUT_MODULE_ID:
        subprocess.run(["pactl", "unload-module", OUTPUT_MODULE_ID], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        print("[PulseAudio] Unloaded 'Audio_Stream_Output'.")
        OUTPUT_MODULE_ID = None

    if CLEANUP_ALL and OBS_MODULE_ID:
        subprocess.run(["pactl", "unload-module", OBS_MODULE_ID], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        print("[PulseAudio] Unloaded 'OBS_Input'.")
        OBS_MODULE_ID = None
    elif OBS_MODULE_ID:
        print("[PulseAudio] Preserved 'OBS_Input' (OBS monitoring device will NOT reset).")


atexit.register(cleanup_pulse_sinks)

# PulseAudio environment configuration
os.environ["PULSE_SOURCE"] = "OBS_Input.monitor"
os.environ["PULSE_SINK"] = "Audio_Stream_Output"
os.environ["PULSE_PROP_application.name"] = "Audio_Bridge_Primary"
os.environ["PULSE_PROP_media.name"] = "Audio_Bridge_Primary"

SAMPLE_RATE = 48000
BLOCK_SIZE = 1024
CHANNELS = 2

monitor_queue = queue.Queue(maxsize=10)


def bridge_callback(indata, outdata, frames, time_info, status):
    if indata is not None and len(indata) > 0:
        outdata[:] = indata
        try:
            monitor_queue.put_nowait(indata.copy())
        except queue.Full:
            try:
                _ = monitor_queue.get_nowait()
                monitor_queue.put_nowait(indata.copy())
            except queue.Empty:
                pass
    else:
        outdata.fill(0)


def monitor_callback(outdata, frames, time_info, status):
    try:
        data = monitor_queue.get_nowait()
        if outdata.shape[1] == data.shape[1]:
            outdata[:] = data
        elif outdata.shape[1] == 1 and data.shape[1] == 2:
            outdata[:] = data.mean(axis=1, keepdims=True)
        else:
            outdata.fill(0)
    except queue.Empty:
        outdata.fill(0)


def list_output_devices():
    devices = sd.query_devices()
    valid_ids = []
    print("\n" + "=" * 58)
    print("Available Output Devices:")
    print("=" * 58)
    for idx, dev in enumerate(devices):
        if dev['max_output_channels'] > 0:
            valid_ids.append(idx)
            host_api = sd.query_hostapis(dev['hostapi'])['name']
            print(f" [{idx:2d}] {dev['name']} ({host_api}) - {dev['max_output_channels']} out")
    print("=" * 58)
    return valid_ids


def get_default_pulse_device():
    devices = sd.query_devices()
    for idx, dev in enumerate(devices):
        name = dev['name'].lower()
        if "pulse" in name or "pipewire" in name:
            return idx
    return sd.default.device[0]


class MonitorController:
    def __init__(self, fallback_pulse_id):
        self.stream = None
        self.current_device = None
        self.mode = "none"
        self.fallback_pulse_id = fallback_pulse_id
        self.lock = threading.Lock()

    def set_to_none(self):
        with self.lock:
            self._close_stream()
            try:
                self.stream = sd.OutputStream(
                    device=self.fallback_pulse_id,
                    samplerate=SAMPLE_RATE,
                    blocksize=BLOCK_SIZE,
                    channels=CHANNELS,
                    dtype='float32',
                    callback=monitor_callback
                )
                self.stream.start()
                self.mode = "none"
                self.current_device = self.fallback_pulse_id
                print(f"[Monitor] Set to 'none': Mirrored to Audio_Stream_Output [{self.fallback_pulse_id}].")
                return True
            except Exception as e:
                print(f"[Error] Failed to bind secondary stream to Pulse sink: {e}")
                return False

    def start_device(self, device_id):
        with self.lock:
            self._close_stream()
            try:
                dev_info = sd.query_devices(device_id)
                target_channels = min(CHANNELS, dev_info['max_output_channels'])

                self.stream = sd.OutputStream(
                    device=device_id,
                    samplerate=SAMPLE_RATE,
                    blocksize=BLOCK_SIZE,
                    channels=target_channels,
                    dtype='float32',
                    callback=monitor_callback
                )
                self.stream.start()
                self.mode = "custom"
                self.current_device = device_id
                print(f"[Monitor] Streaming to: [{device_id}] {dev_info['name']}")
                return True
            except Exception as e:
                print(f"[Error] Failed to connect to device {device_id}: {e}")
                self._close_stream()
                return False

    def _close_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
            self.current_device = None

    def stop(self):
        with self.lock:
            self._close_stream()


def select_device_flow(monitor_mgr):
    """Stage 1: Prompt for initial device configuration."""
    while True:
        list_output_devices()
        print("\nSelect Monitor Device: [ID] | 'none' (mirror to Audio_Stream_Output) | 'ls' (refresh)")
        sys.stdout.write("Device Choice > ")
        sys.stdout.flush()

        user_input = sys.stdin.readline().strip().lower()
        if not user_input:
            continue
        if user_input == 'ls':
            continue
        if user_input in ('none', 'off'):
            monitor_mgr.set_to_none()
            break

        try:
            chosen_id = int(user_input)
            valid_ids = [i for i, d in enumerate(sd.query_devices()) if d['max_output_channels'] > 0]
            if chosen_id in valid_ids:
                if monitor_mgr.start_device(chosen_id):
                    break
            else:
                print(f"[!] Invalid ID {chosen_id}.")
        except ValueError:
            print("[!] Invalid input. Enter an ID, 'none', or 'ls'.")


def quit_control_loop(stop_event):
    """Stage 2: Strictly listens for quit/cleanup controls."""
    global CLEANUP_ALL

    print("\n" + "-" * 58)
    print("Bridge is active and running.")
    print("Controls:")
    print("  - 'q'  : Quit (keep OBS sink intact)")
    print("  - 'qc' : Quit and Clean all sinks")
    print("-" * 58)

    while not stop_event.is_set():
        try:
            sys.stdout.write("\nControl ('q' / 'qc') > ")
            sys.stdout.flush()
            user_input = sys.stdin.readline().strip().lower()

            if not user_input:
                continue

            if user_input == 'q':
                CLEANUP_ALL = False
                stop_event.set()
                break
            elif user_input == 'qc':
                CLEANUP_ALL = True
                stop_event.set()
                break
            else:
                print("[!] Invalid command. Use 'q' to quit or 'qc' to quit and clean.")

        except (EOFError, KeyboardInterrupt):
            CLEANUP_ALL = False
            stop_event.set()
            break


def main():
    setup_pulse_sinks()

    pulse_idx = get_default_pulse_device()
    print(f"[Info] Pulse/PipeWire device index: {pulse_idx}")

    monitor_mgr = MonitorController(fallback_pulse_id=pulse_idx)
    stop_event = threading.Event()

    try:
        with sd.Stream(
            device=(pulse_idx, pulse_idx),
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            channels=CHANNELS,
            dtype='float32',
            callback=bridge_callback
        ):
            print("\n[Primary Bridge] Running: OBS_Input.monitor -> Audio_Stream_Output")

            # Stage 1: Device Selection
            select_device_flow(monitor_mgr)

            # Stage 2: Quit Control loop
            cmd_thread = threading.Thread(target=quit_control_loop, args=(stop_event,), daemon=True)
            cmd_thread.start()

            while not stop_event.is_set():
                time.sleep(0.2)

    except KeyboardInterrupt:
        print("\nStopping audio bridge...")
    finally:
        stop_event.set()
        monitor_mgr.stop()
        cleanup_pulse_sinks()
        print("Shutdown complete.")


if __name__ == "__main__":
    main()