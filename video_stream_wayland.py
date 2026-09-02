#!/usr/bin/env python3
"""
V4L2 Raw MJPEG Recorder & Delayed Playback Monitor
- Wayland Native frameless window.
- Saves raw MJPEG and V4L2 timestamps into a dedicated folder.
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
import sys
import time
import cv2
import numpy as np

os.environ["QT_LOGGING_RULES"] = "qt.qpa.wayland*=false;*.debug=false"

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
except ImportError:
    print("[!] Error: PyQt5 is required. Run: pip install PyQt5")
    sys.exit(1)

# ==============================================================================
# Linux V4L2 Kernel Definitions
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
# USB Helper Functions
# ==============================================================================
def get_usb_info(video_name_file):
  video_dir = os.path.dirname(video_name_file)
  device_link = os.path.join(video_dir, 'device')
  if not os.path.exists(device_link): return None
  usb_device_dir = os.path.dirname(os.path.realpath(device_link))
  try:
      with open(os.path.join(usb_device_dir, 'busnum')) as f: bus = int(f.read().strip())
      with open(os.path.join(usb_device_dir, 'devnum')) as f: dev = int(f.read().strip())
      with open(os.path.join(usb_device_dir, 'devpath')) as f: devpath = f.read().strip()
      return {'bus': bus, 'dev': dev, 'devpath': devpath}
  except Exception: return None

def find_device_node_by_usb(target_bus, target_dev=None, target_devpath=None):
  for name_file in sorted(glob.glob('/sys/class/video4linux/video*/name')):
    usb_info = get_usb_info(name_file)
    if not usb_info: continue
    match = False
    if target_devpath is not None:
        if usb_info['bus'] == target_bus and usb_info['devpath'] == target_devpath: match = True
    elif target_dev is not None:
        if usb_info['bus'] == target_bus and usb_info['dev'] == target_dev: match = True
    if match:
        dev_node = f"/dev/{os.path.basename(os.path.dirname(name_file))}"
        with open(name_file, 'r') as f: name = f.read().strip()
        return dev_node, name, usb_info
  return None, None, None

def list_available_cameras():
  print('\nAvailable V4L2 USB Cameras:')
  found = False
  for name_file in sorted(glob.glob('/sys/class/video4linux/video*/name')):
    try:
      usb_info = get_usb_info(name_file)
      if not usb_info: continue
      with open(name_file, 'r') as f: name = f.read().strip()
      dev = f"/dev/{os.path.basename(os.path.dirname(name_file))}"
      print(f"  • Bus {usb_info['bus']:03d}, Device {usb_info['dev']:03d} [Port {usb_info['devpath']}] -> {dev} ('{name}')")
      found = True
    except Exception: pass
  if not found: print("  (No USB cameras found)")
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
    
    self.actual_w, self.actual_h = fmt.pix.width, fmt.pix.height

    req = v4l2_requestbuffers()
    req.count = self.num_buffers
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
    req.memory = V4L2_MEMORY_MMAP
    fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req)

    for i in range(req.count):
      buf = v4l2_buffer()
      buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
      buf.memory = V4L2_MEMORY_MMAP
      buf.index = i
      fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, buf)
      mm = mmap.mmap(self.fd, buf.length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=buf.offset)
      self.buffers.append((mm, buf.length))
      fcntl.ioctl(self.fd, VIDIOC_QBUF, buf)

    buf_type = struct.pack('I', V4L2_BUF_TYPE_VIDEO_CAPTURE)
    fcntl.ioctl(self.fd, VIDIOC_STREAMON, buf_type)

  def read_frame(self, timeout=2.0):
    r, _, _ = select.select([self.fd], [], [], timeout)
    if not r: raise TimeoutError('Camera frame timeout.')
    buf = v4l2_buffer()
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
    buf.memory = V4L2_MEMORY_MMAP
    fcntl.ioctl(self.fd, VIDIOC_DQBUF, buf)
    
    # Extract raw bytes AND accurate V4L2 hardware timestamp
    raw_bytes = self.buffers[buf.index][0][: buf.bytesused]
    v4l2_timestamp = buf.timestamp.tv_sec + (buf.timestamp.tv_usec / 1_000_000.0)
    
    fcntl.ioctl(self.fd, VIDIOC_QBUF, buf)
    return raw_bytes, v4l2_timestamp

  def stop(self):
    if self.fd is not None:
      try: fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, struct.pack('I', V4L2_BUF_TYPE_VIDEO_CAPTURE))
      except Exception: pass
      for mm, _ in self.buffers:
        try: mm.close()
        except Exception: pass
      self.buffers.clear()
      try: os.close(self.fd)
      except Exception: pass
      self.fd = None

# ==============================================================================
# Wayland Native Borderless UI (PyQt5)
# ==============================================================================
class VideoWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.has_borders = False
        self.apply_window_flags()
        self.setStyleSheet("background-color: black;")
        
        self.layout = QtWidgets.QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        
        self.label = QtWidgets.QLabel(self)
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        self.layout.addWidget(self.label)
        
        self.delay_sec = 2.0
        self.exit_req = False
        self.is_fullscreen = False
        self.current_w = 0
        self.current_h = 0

    def apply_window_flags(self):
        flags = QtCore.Qt.Window
        if not self.has_borders:
            flags |= QtCore.Qt.FramelessWindowHint
        self.setWindowFlags(flags)

    def set_video_size(self, w, h):
        if self.current_w != w or self.current_h != h:
            self.current_w = w
            self.current_h = h
            if not self.is_fullscreen:
                self.setFixedSize(w, h)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Q, QtCore.Qt.Key_Escape):
            self.exit_req = True
        elif event.key() in (QtCore.Qt.Key_Plus, QtCore.Qt.Key_Equal):
            self.delay_sec = min(10.0, self.delay_sec + 0.5)
        elif event.key() in (QtCore.Qt.Key_Minus, QtCore.Qt.Key_Underscore):
            self.delay_sec = max(0.0, self.delay_sec - 0.5)
        elif event.key() == QtCore.Qt.Key_B:
            if not self.is_fullscreen:
                self.has_borders = not self.has_borders
                self.apply_window_flags()
                self.show()  
        elif event.key() == QtCore.Qt.Key_F:
            self.is_fullscreen = not self.is_fullscreen
            if self.is_fullscreen:
                self.setMinimumSize(0, 0)
                self.setMaximumSize(16777215, 16777215)
                self.showFullScreen()
            else:
                self.showNormal()
                self.setFixedSize(self.current_w, self.current_h)

    def update_frame(self, frame_bgr):
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        h, w, ch = frame_rgb.shape
        bytes_per_line = ch * w
        qimg = QtGui.QImage(frame_rgb.data, w, h, bytes_per_line, QtGui.QImage.Format_RGB888)
        
        pixmap = QtGui.QPixmap.fromImage(qimg).scaled(
            self.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
        )
        self.label.setPixmap(pixmap)

# ==============================================================================
# Main Stream Loop
# ==============================================================================
def run_stream(target_bus, target_dev, requested_width=1920, requested_height=1080):
  app = QtWidgets.QApplication(sys.argv)
  win = VideoWindow()
  win.show()

  # Create a dedicated directory for this capture session
  timestamp_str = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
  save_dir = f"capture_{timestamp_str}"
  os.makedirs(save_dir, exist_ok=True)
  
  out_filename = os.path.join(save_dir, 'video.mjpeg')
  ts_filename = os.path.join(save_dir, 'timestamps.txt')
  
  out_file = open(out_filename, 'wb')
  ts_file = open(ts_filename, 'w')
  print(f"\n[*] Recording to folder: '{save_dir}/'")

  frame_buffer = collections.deque(maxlen=1000)
  cap = None
  node_path = None
  locked_devpath = None

  try:
    while not win.exit_req:
      # --- Stage 1: Device Discovery & Reconnect ---
      if cap is None:
        if locked_devpath:
            node_path, found_name, usb_info = find_device_node_by_usb(target_bus, target_devpath=locked_devpath)
            sys.stdout.write(f"\r[!] Waiting for camera on Bus {target_bus} Port {locked_devpath}... ")
        else:
            node_path, found_name, usb_info = find_device_node_by_usb(target_bus, target_dev=target_dev)
            sys.stdout.write(f"\r[!] Searching for camera Bus {target_bus} Device {target_dev}... ")

        sys.stdout.flush()
        
        if not node_path:
          end_time = time.time() + 0.7
          while time.time() < end_time:
              app.processEvents()
              time.sleep(0.05)
          continue
        
        if not locked_devpath:
            locked_devpath = usb_info['devpath']
            print(f"\n[+] Locked onto physical USB Port: {locked_devpath}")

        print(f"\n[+] Found '{found_name}' on {node_path}. Initializing...")
        try:
          cap = V4L2MJPEGCapture(node_path, width=requested_width, height=requested_height)
          cap.start()
          
          # Force 1920x1080 as requested
          win.set_video_size(1920, 1080)
          print(f'[+] Stream started at {cap.actual_w}x{cap.actual_h}\n')
        except Exception as e:
          print(f'\n[X] Initialization error: {e}')
          if cap:
            cap.stop()
            cap = None
          app.processEvents()
          input("\n[TERMINAL] Press [ENTER] to retry opening the device...")
          continue

      # --- Stage 2: Capture, Save, and Delayed Playback ---
      try:
        # Read frame and hardware timestamp
        raw_mjpeg, v4l2_ts = cap.read_frame(timeout=2.0)
        capture_time = time.time()
        
        # Save frame bytes and write timestamp to txt file
        out_file.write(raw_mjpeg)
        ts_file.write(f"{v4l2_ts:.6f}\n")

        np_arr = np.frombuffer(raw_mjpeg, dtype=np.uint8)
        frame_bgr = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

        if frame_bgr is not None:
          frame_buffer.append((capture_time, frame_bgr))

        now = time.time()
        target_time = now - win.delay_sec

        while len(frame_buffer) > 1 and frame_buffer[1][0] <= target_time:
          frame_buffer.popleft()

        if frame_buffer:
          display_frame = frame_buffer[0][1]
          actual_delay = now - frame_buffer[0][0]
        else:
          display_frame = np.zeros((cap.actual_h, cap.actual_w, 3), dtype=np.uint8)
          actual_delay = 0.0

        win.update_frame(display_frame)
        app.processEvents()
        
        file_size_mb = out_file.tell() / (1024 * 1024)

        sys.stdout.write(
            f"\r[>] Tgt: {win.delay_sec:04.1f}s | Real: {actual_delay:05.2f}s | "
            f"Size: {file_size_mb:05.1f}MB [Keys: +/-, b, f, q]   "
        )
        sys.stdout.flush()

      except (OSError, IOError, TimeoutError) as err:
        print() 
        if not os.path.exists(node_path):
          print(f'\n[!] Camera disconnected! Polling Port {locked_devpath} for reconnection...')
          if cap:
            cap.stop()
            cap = None
          frame_buffer.clear()
          end_time = time.time() + 0.7
          while time.time() < end_time:
              app.processEvents()
              time.sleep(0.05)
        else:
          print(f'\n[!] Stream Error: {err}')
          if cap:
            cap.stop()
            cap = None
          frame_buffer.clear()
          app.processEvents()
          input('\n[TERMINAL] Press [ENTER] to stop & restart stream...')

  finally:
    if cap: cap.stop()
    out_file.close()
    ts_file.close()
    print(f"\n\n[+] Recording safely saved in: '{save_dir}/'")

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

  run_stream(target_bus, target_dev, requested_width=1920, requested_height=1080)