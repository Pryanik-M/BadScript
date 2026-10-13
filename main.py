"""
main.py — точка входа BadScript.
Позволяет запустить веб-панель Django (для VPS) или агент (для управляемого ПК).
"""
import sys
import subprocess
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def show_help():
    print("=" * 60)
    print("  BadScript — Комплекс удалённого администрирования")
    print("=" * 60)
    print("Использование:")
    print("  1) Запуск веб-панели (на VPS):")
    print("     python main.py panel [--port 8000]")
    print()
    print("  2) Запуск агента (на подчиненном ПК):")
    print("     python main.py agent --vps ws://VPS_IP:8000/ws/agent/ --uid <UID> --secret <TOKEN>")
    print()
    print("  3) Прямой запуск компонентов:")
    print("     cd panel && python manage.py runserver 0.0.0.0:8000")
    print("     python agent/agent.py --vps ws://VPS_IP:8000/ws/agent/ --uid <UID> --secret <TOKEN>")
    print("=" * 60)

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        show_help()
        return

    cmd = sys.argv[1].lower()
    if cmd == "panel":
        port = "8000"
        if "--port" in sys.argv:
            idx = sys.argv.index("--port")
            if idx + 1 < len(sys.argv):
                port = sys.argv[idx + 1]
        panel_dir = os.path.join(BASE_DIR, "panel")
        print(f"[*] Запуск Django Web Panel на порту {port}...")
        sys.exit(subprocess.call([
            sys.executable, "-m", "daphne",
            "-b", "0.0.0.0", "-p", str(port),
            "panel.asgi:application"
        ], cwd=panel_dir))

    elif cmd == "agent":
        agent_script = os.path.join(BASE_DIR, "agent", "agent.py")
        sys.exit(subprocess.call([
            sys.executable, agent_script
        ] + sys.argv[2:]))

    else:
        show_help()

if __name__ == "__main__":
    main()
