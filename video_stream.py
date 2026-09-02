#!/usr/bin/env python3
"""
V4L2 Raw MJPEG Recorder & Delayed Playback Monitor
- Zero re-encoding: Saves raw MJPEG frames straight from V4L2 mmap buffers.
- Port-locking: Survives disconnects by tracking physical USB port paths.
- Clean Video UI: Fixed aspect ratio, removed GUI toolbars.
- Terminal UI: Real-time delay statistics and LIVE storage usage tracking.
"""

import collections
import ctypes
import datetime
import fcntl
import glob
import mmap
import os
import select
import struct
import subprocess
import sys
import time
import cv2
import numpy as np

# ==============================================================================
# Linux V4L2 Kernel Definitions (ctypes)
# ==============================================================================
VIDIOC_S_FMT = 0xC0D05605
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF = 0x40045613

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP = 1
V4L2_FIELD_ANY = 0


def v4l2_fourcc(a, b, c, d):
  return (ord(a) | (ord(b) << 8) | (ord(c) << 16) | (ord(d) << 24))

V4L2_PIX_FMT_MJPEG = v4l2_fourcc('M', 'J', 'P', 'G')

class v4l2_pix_format(ctypes.Structure):
  _fields_ = [
      ('width', ctypes.c_uint32), ('height', ctypes.c_uint32),
      ('pixelformat', ctypes.c_uint32), ('field', ctypes.c_uint32),
      ('bytesperline', ctypes.c_uint32), ('sizeimage', ctypes.c_uint32),
      ('colorspace', ctypes.c_uint32), ('priv', ctypes.c_uint32),
      ('flags', ctypes.c_uint32), ('ycbcr_enc', ctypes.c_uint32),
      ('quantization', ctypes.c_uint32), ('xfer_func', ctypes.c_uint32),
  ]

class v4l2_format(ctypes.Structure):
  class _u(ctypes.Union):
    _fields_ = [('pix', v4l2_pix_format), ('raw_data', ctypes.c_uint8 * 200)]
  _anonymous_ = ('u',)
  _fields_ = [('type', ctypes.c_uint32), ('u', _u)]

class v4l2_requestbuffers(ctypes.Structure):
  _fields_ = [
      ('count', ctypes.c_uint32), ('type', ctypes.c_uint32),
      ('memory', ctypes.c_uint32), ('reserved', ctypes.c_uint32 * 2),
  ]

class v4l2_timeval(ctypes.Structure):
  _fields_ = [('tv_sec', ctypes.c_long), ('tv_usec', ctypes.c_long)]

class v4l2_buffer(ctypes.Structure):
  class _u(ctypes.Union):
    _fields_ = [('offset', ctypes.c_uint32), ('userptr', ctypes.c_ulong)]
  _anonymous_ = ('m',)
  _fields_ = [
      ('index', ctypes.c_uint32), ('type', ctypes.c_uint32),
      ('bytesused', ctypes.c_uint32), ('flags', ctypes.c_uint32),
      ('field', ctypes.c_uint32), ('timestamp', v4l2_timeval),
      ('timecode', ctypes.c_uint32 * 4), ('sequence', ctypes.c_uint32),
      ('memory', ctypes.c_uint32), ('m', _u),
      ('length', ctypes.c_uint32), ('reserved2', ctypes.c_uint32),
      ('reserved', ctypes.c_uint32),
  ]

# ==============================================================================
# USB & Sysfs Helper Functions
# ==============================================================================
def get_usb_info(video_name_file):
  """Extracts USB Bus, Device, and Port Path for a /dev/videoX node."""
  video_dir = os.path.dirname(video_name_file)
  device_link = os.path.join(video_dir, 'device')
  
  if not os.path.exists(device_link):
      return None
  
  usb_device_dir = os.path.dirname(os.path.realpath(device_link))
  
  try:
      with open(os.path.join(usb_device_dir, 'busnum')) as f:
          bus = int(f.read().strip())
      with open(os.path.join(usb_device_dir, 'devnum')) as f:
          dev = int(f.read().strip())
      with open(os.path.join(usb_device_dir, 'devpath')) as f:
          devpath = f.read().strip()
          
      return {'bus': bus, 'dev': dev, 'devpath': devpath}
  except Exception:
      return None

def find_device_node_by_usb(target_bus, target_dev=None, target_devpath=None):
  for name_file in sorted(glob.glob('/sys/class/video4linux/video*/name')):
    usb_info = get_usb_info(name_file)
    if not usb_info: 
        continue
    
    match = False
    if target_devpath is not None:
        if usb_info['bus'] == target_bus and usb_info['devpath'] == target_devpath:
            match = True
    elif target_dev is not None:
        if usb_info['bus'] == target_bus and usb_info['dev'] == target_dev:
            match = True
            
    if match:
        dev_node = f"/dev/{os.path.basename(os.path.dirname(name_file))}"
        with open(name_file, 'r') as f:
            name = f.read().strip()
        return dev_node, name, usb_info
        
  return None, None, None

def list_available_cameras():
  print('\nAvailable V4L2 USB Cameras:')
  found = False
  for name_file in sorted(glob.glob('/sys/class/video4linux/video*/name')):
    try:
      usb_info = get_usb_info(name_file)
      if not usb_info: continue
      
      with open(name_file, 'r') as f:
        name = f.read().strip()
        
      dev = f"/dev/{os.path.basename(os.path.dirname(name_file))}"
      print(f"  • Bus {usb_info['bus']:03d}, Device {usb_info['dev']:03d} "
            f"[Port {usb_info['devpath']}] -> {dev} ('{name}')")
      found = True
    except Exception:
      pass
  if not found:
    print("  (No USB cameras found)")
  print()

# ==============================================================================
# V4L2 Direct MJPEG Capture Engine
# ==============================================================================
class V4L2MJPEGCapture:
  def __init__(self, dev_node, width=1920, height=1080, num_buffers=4):
    self.dev_node = dev_node
    self.width = width
    self.height = height
    self.num_buffers = num_buffers
    self.fd = None
    self.buffers = []

  def start(self):
    self.fd = os.open(self.dev_node, os.O_RDWR | os.O_NONBLOCK, 0)

    fmt = v4l2_format()
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
    fmt.pix.width = self.width
    fmt.pix.height = self.height
    fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG
    fmt.pix.field = V4L2_FIELD_ANY
    fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)

    self.actual_w = fmt.pix.width
    self.actual_h = fmt.pix.height

    req = v4l2_requestbuffers()
    req.count = self.num_buffers
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
    req.memory = V4L2_MEMORY_MMAP
    fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req)

    self.buffers = []
    for i in range(req.count):
      buf = v4l2_buffer()
      buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
      buf.memory = V4L2_MEMORY_MMAP
      buf.index = i
      fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, buf)

      mm = mmap.mmap(
          self.fd, buf.length, mmap.MAP_SHARED,
          mmap.PROT_READ | mmap.PROT_WRITE, offset=buf.offset,
      )
      self.buffers.append((mm, buf.length))
      fcntl.ioctl(self.fd, VIDIOC_QBUF, buf)

    buf_type = struct.pack('I', V4L2_BUF_TYPE_VIDEO_CAPTURE)
    fcntl.ioctl(self.fd, VIDIOC_STREAMON, buf_type)

  def read_frame(self, timeout=2.0):
    r, _, _ = select.select([self.fd], [], [], timeout)
    if not r:
      raise TimeoutError('Camera frame timeout (no data received).')

    buf = v4l2_buffer()
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
    buf.memory = V4L2_MEMORY_MMAP
    fcntl.ioctl(self.fd, VIDIOC_DQBUF, buf)

    raw_bytes = self.buffers[buf.index][0][: buf.bytesused]
    fcntl.ioctl(self.fd, VIDIOC_QBUF, buf)
    return raw_bytes

  def stop(self):
    if self.fd is not None:
      try:
        buf_type = struct.pack('I', V4L2_BUF_TYPE_VIDEO_CAPTURE)
        fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, buf_type)
      except Exception: pass
      for mm, _ in self.buffers:
        try: mm.close()
        except Exception: pass
      self.buffers.clear()
      try: os.close(self.fd)
      except Exception: pass
      self.fd = None

# ==============================================================================
# Main Stream Loop
# ==============================================================================
def run_stream(target_bus, target_dev, width=1920, height=1080):
  window_name = 'Playback'
  
  cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE | cv2.WINDOW_GUI_NORMAL)

  delay_sec = 2.0
  max_delay_sec = 10.0
  is_fullscreen = False
  decorations_removed = False  # Track if we have stripped the title bar

  timestamp_str = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
  out_filename = f'capture_{timestamp_str}.mjpeg'
  out_file = open(out_filename, 'wb')
  print(f"\n[*] Raw MJPEG recording to: '{out_filename}' (Zero re-encoding)")

  frame_buffer = collections.deque(maxlen=1000)
  cap = None
  node_path = None
  locked_devpath = None

  try:
    while True:
      # --- Stage 1: Device Discovery & Reconnect ---
      if cap is None:
        if locked_devpath:
            node_path, found_name, usb_info = find_device_node_by_usb(target_bus, target_devpath=locked_devpath)
            status_msg = f"\r[!] Waiting for camera on Bus {target_bus} Port {locked_devpath}..."
        else:
            node_path, found_name, usb_info = find_device_node_by_usb(target_bus, target_dev=target_dev)
            status_msg = f"\r[!] Searching for camera Bus {target_bus} Device {target_dev}..."

        if not node_path:
          sys.stdout.write(status_msg)
          sys.stdout.flush()
          time.sleep(0.7)
          continue
        
        if not locked_devpath:
            locked_devpath = usb_info['devpath']
            print(f"\n[+] Locked onto physical USB Port: {locked_devpath} (survives disconnects)")

        print(f"\n[+] Found '{found_name}' on {node_path}. Initializing...")
        try:
          cap = V4L2MJPEGCapture(node_path, width=width, height=height)
          cap.start()
          print(f'[+] Stream started at {cap.actual_w}x{cap.actual_h}\n')
        except Exception as e:
          print(f'\n[X] Initialization error: {e}')
          if cap:
            cap.stop()
            cap = None
          input("\n[TERMINAL] Press [ENTER] to retry opening the device...")
          continue

      # --- Stage 2: Capture, Save, and Delayed Playback ---
      try:
        raw_mjpeg = cap.read_frame(timeout=2.0)
        capture_time = time.time()
        
        out_file.write(raw_mjpeg)

        np_arr = np.frombuffer(raw_mjpeg, dtype=np.uint8)
        frame_bgr = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

        if frame_bgr is not None:
          frame_buffer.append((capture_time, frame_bgr))

        now = time.time()
        target_time = now - delay_sec

        while len(frame_buffer) > 1 and frame_buffer[1][0] <= target_time:
          frame_buffer.popleft()

        if frame_buffer:
          display_frame = frame_buffer[0][1].copy()
          actual_delay = now - frame_buffer[0][0]
        else:
          display_frame = np.zeros((480, 640, 3), dtype=np.uint8)
          actual_delay = 0.0

        cv2.imshow(window_name, display_frame)
        
        # --- STRIP OS TITLE BAR HACK ---
        # We must wait until after cv2.imshow has executed at least once so the OS window exists.
        if not decorations_removed:
            try:
                # Use X11 Motif Hints to tell the window manager to remove the title bar borders
                subprocess.Popen(
                    ['xprop', '-name', window_name, '-f', '_MOTIF_WM_HINTS', '32c', 
                     '-set', '_MOTIF_WM_HINTS', '0x2, 0x0, 0x0, 0x0, 0x0'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except FileNotFoundError:
                pass # xprop is not installed on this system
            decorations_removed = True
        
        file_size_mb = out_file.tell() / (1024 * 1024)

        sys.stdout.write(
            f"\r[>] Target: {delay_sec:04.1f}s | Real Delay: {actual_delay:05.2f}s | "
            f"Storage: {file_size_mb:07.2f} MB  "
            f"(Keys: '+' / '-' delay, 'f' fullscreen, 'q' exit)   "
        )
        sys.stdout.flush()

        key = cv2.waitKey(1) & 0xFF
        if key in (ord('q'), 27):
          print('\n\n[*] Exit requested by user.')
          break
        elif key in (ord('+'), ord('=')):
          delay_sec = min(max_delay_sec, delay_sec + 0.5)
        elif key in (ord('-'), ord('_')):
          delay_sec = max(0.0, delay_sec - 0.5)
        elif key == ord('f'):
          is_fullscreen = not is_fullscreen
          if is_fullscreen:
              cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
          else:
              cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_NORMAL)

      except (OSError, IOError, TimeoutError) as err:
        print() 
        if not os.path.exists(node_path):
          print(f'\n[!] Camera disconnected! Polling physical Port {locked_devpath} for reconnection...')
          if cap:
            cap.stop()
            cap = None
          frame_buffer.clear()
          time.sleep(0.7)
        else:
          print(f'\n[!] Stream Error: {err}')
          if cap:
            cap.stop()
            cap = None
          frame_buffer.clear()
          input('\n[TERMINAL] Press [ENTER] to stop & restart stream...')

  finally:
    if cap: cap.stop()
    out_file.close()
    cv2.destroyAllWindows()
    print(f"\n[+] Recording safely saved: '{out_filename}'")

# ==============================================================================
# Entry Point
# ==============================================================================
if __name__ == '__main__':
  list_available_cameras()

  user_input = input("Enter USB Bus and Device separated by a space (e.g., '1 5'): ").strip()
  try:
    bus_str, dev_str = user_input.split()
    target_bus = int(bus_str)
    target_dev = int(dev_str)
  except ValueError:
    print("Invalid format. Please enter two numbers separated by a space.")
    sys.exit(1)

  run_stream(target_bus, target_dev, width=1920, height=1080)