import os
import sys
import numpy as np
import sounddevice as sd

# Setup environment variables for the primary stream (Vesktop)
os.environ["PULSE_SOURCE"] = "OBS_Input.monitor"  # Read from OBS
os.environ["PULSE_SINK"] = "Vesktop_Out"          # Play to Vesktop
os.environ["PULSE_PROP_application.name"] = "OBS_Stream"
os.environ["PULSE_PROP_media.name"] = "OBS_Stream"

SAMPLE_RATE = 48000
BLOCK_SIZE = 1024
CHANNELS = 2

# Device 5 = pulse
PULSE_DEVICE = 5

# Global buffer to mirror audio
mirror_buffer = np.zeros((BLOCK_SIZE, CHANNELS), dtype='float32')

def bridge_callback(indata, outdata, frames, time, status):
    global mirror_buffer
    if indata is not None and len(indata) > 0:
        outdata[:] = indata
        mirror_buffer = indata.copy()
    else:
        outdata.fill(0)
        mirror_buffer.fill(0)

def monitor_callback(outdata, frames, time, status):
    global mirror_buffer
    outdata[:] = mirror_buffer

def main():
    print("Starting dual-output bridge...")
    print("Vesktop gets: OBS_Feed")
    print("You can route the secondary playback in pavucontrol to your headphones.")

    try:
        # Primary stream: OBS_Feed.monitor -> OBS_Feed
        with sd.Stream(
            device=(PULSE_DEVICE, PULSE_DEVICE),
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            channels=CHANNELS,
            dtype='float32',
            callback=bridge_callback
        ):
            # Secondary stream: plays the same data to your headphones
            with sd.OutputStream(
                device=PULSE_DEVICE,
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                channels=CHANNELS,
                dtype='float32',
                callback=monitor_callback
            ):
                print("Running. In pavucontrol Playback tab, point the second Python stream to your headphones.")
                while True:
                    sd.sleep(1000)
    except KeyboardInterrupt:
        print("\nStopping.")

if __name__ == "__main__":
    main()