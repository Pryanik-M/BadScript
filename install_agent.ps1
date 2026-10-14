# ============================================================
# BadScript Agent — PowerShell Установщик для Windows
# ============================================================

$Host.UI.RawUI.WindowTitle = "BadScript Agent Installer"

Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║     BadScript Agent for Windows      ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

$InstallDir = "$env:USERPROFILE\.badscript"
$RawBase = "https://raw.githubusercontent.com/Pryanik-M/BadScript/main/agent"
$VenvDir = "$InstallDir\venv"

# 1. Строгая проверка наличия рабочего Python 3
$pyCmd = $null
$pyVer = $null

# Проверяем python
try {
    $out = & python -c "import sys; assert sys.version_info[0] >= 3; print('Python ' + sys.version.split()[0])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) {
        $pyCmd = "python"
        $pyVer = $out
    }
} catch {}

# Проверяем py launcher
if (!$pyCmd) {
    try {
        $out = & py -3 -c "import sys; assert sys.version_info[0] >= 3; print('Python ' + sys.version.split()[0])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $pyCmd = "py -3"
            $pyVer = $out
        }
    } catch {}
}

if (!$pyCmd) {
    Write-Host "[✗] ОШИБКА: Python 3 не найден или не установлен!" -ForegroundColor Red
    Write-Host ""
    Write-Host "    Для работы агента требуется Python 3.8+:" -ForegroundColor Yellow
    Write-Host "    1. Скачайте Python с официального сайта: https://www.python.org/downloads/"
    Write-Host "    2. При установке ОБЯЗАТЕЛЬНО отметьте галочку 'Add python.exe to PATH'"
    Write-Host "    3. После установки перезапустите этот установщик."
    Write-Host ""
    Read-Host "Нажмите Enter для выхода..."
    exit 1
}

Write-Host "[✓] $pyVer найден ($pyCmd)" -ForegroundColor Green

# 2. Ввод данных подключения
Write-Host ""
Write-Host "Введите данные для подключения агента:" -ForegroundColor Yellow
$VpsUrl = Read-Host "  VPS адрес панели (например ws://192.168.31.100:8000/ws/agent/)"
if ([string]::IsNullOrWhiteSpace($VpsUrl)) {
    Write-Host "[✗] Адрес VPS не может быть пустым!" -ForegroundColor Red
    exit 1
}

$VpsUrl = $VpsUrl.Trim()
if ($VpsUrl -notmatch '^wss?://') {
    if ($VpsUrl -match '^https?://') {
        $VpsUrl = $VpsUrl -replace '^http', 'ws'
    } else {
        $VpsUrl = "ws://$VpsUrl"
    }
}
if ($VpsUrl -notmatch '/ws/agent/?$') {
    $VpsUrl = $VpsUrl.TrimEnd('/') + '/ws/agent/'
}

$Uid = Read-Host "  UID устройства (из панели)"
if ([string]::IsNullOrWhiteSpace($Uid)) {
    Write-Host "[✗] UID не может быть пустым!" -ForegroundColor Red
    exit 1
}

$Secret = Read-Host "  Секретный токен (из панели)"
if ([string]::IsNullOrWhiteSpace($Secret)) {
    Write-Host "[✗] Токен не может быть пустым!" -ForegroundColor Red
    exit 1
}

# 3. Скачивание ТОЛЬКО файлов агента (без файлов Django)
if (!(Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}

Write-Host ""
Write-Host "[→] Скачивание файлов агента с GitHub..." -ForegroundColor Cyan

try {
    Invoke-WebRequest -Uri "$RawBase/agent.py" -OutFile "$InstallDir\agent.py" -UseBasicParsing
    Invoke-WebRequest -Uri "$RawBase/shared.py" -OutFile "$InstallDir\shared.py" -UseBasicParsing
    Invoke-WebRequest -Uri "$RawBase/requirements.txt" -OutFile "$InstallDir\requirements.txt" -UseBasicParsing
} catch {
    Write-Host "[✗] Ошибка при скачивании файлов: $_" -ForegroundColor Red
    exit 1
}

if (!(Test-Path "$InstallDir\agent.py")) {
    Write-Host "[✗] Ошибка: agent.py не был сохранён!" -ForegroundColor Red
    exit 1
}

Write-Host "[✓] Файлы агента сохранены в: $InstallDir" -ForegroundColor Green

# 4. Виртуальное окружение
Write-Host "[→] Создание виртуального окружения Python..." -ForegroundColor Cyan
if (!(Test-Path "$VenvDir\Scripts\python.exe")) {
    if ($pyCmd -eq "py -3") {
        & py -3 -m venv $VenvDir
    } else {
        & python -m venv $VenvDir
    }
}

if (!(Test-Path "$VenvDir\Scripts\python.exe")) {
    Write-Host "[✗] Не удалось создать виртуальное окружение venv!" -ForegroundColor Red
    exit 1
}

Write-Host "[→] Установка библиотек..." -ForegroundColor Cyan
& "$VenvDir\Scripts\python.exe" -m pip install --upgrade pip -q
& "$VenvDir\Scripts\pip.exe" install -q websockets cryptography mss pillow psutil pynput numpy opencv-python pyautogui keyboard sounddevice

Write-Host "[✓] Все библиотеки установлены" -ForegroundColor Green

# 5. Конфиг
@"
VPS_URL=$VpsUrl
DEVICE_UID=$Uid
DEVICE_SECRET=$Secret
"@ | Out-File -FilePath "$InstallDir\config.env" -Encoding utf8

# 6. Лаунчеры
$RunBat = @"
@echo off
cd /d "$InstallDir"
start "" "$VenvDir\Scripts\pythonw.exe" "$InstallDir\agent.py" --vps "$VpsUrl" --uid "$Uid" --secret "$Secret" --watchdog --hidden
"@
$RunBat | Out-File -FilePath "$InstallDir\run.bat" -Encoding ascii

$RunConsoleBat = @"
@echo off
cd /d "$InstallDir"
"$VenvDir\Scripts\python.exe" "$InstallDir\agent.py" --vps "$VpsUrl" --uid "$Uid" --secret "$Secret" --watchdog
pause
"@
$RunConsoleBat | Out-File -FilePath "$InstallDir\run_console.bat" -Encoding ascii

# 7. Автозапуск
Write-Host ""
$Autostart = Read-Host "Установить автозапуск при входе в систему? [Y/n]"
if ($Autostart -ne "n" -and $Autostart -ne "N") {
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "BadScriptAgent" -Value "`"$InstallDir\run.bat`""
    Write-Host "[✓] Автозапуск добавлен в реестр (HKCU Run)" -ForegroundColor Green
}

Write-Host ""
Write-Host "══════════════════════════════════════" -ForegroundColor Green
Write-Host "     Установка успешно завершена!     " -ForegroundColor Green
Write-Host "══════════════════════════════════════" -ForegroundColor Green
Write-Host "Папка:  $InstallDir"
Write-Host "Запуск: $InstallDir\run.bat (фоновый режим)"
Write-Host "        $InstallDir\run_console.bat (с выводом в окно)"
Write-Host ""

$RunNow = Read-Host "Запустить агент прямо сейчас? [Y/n]"
if ($RunNow -ne "n" -and $RunNow -ne "N") {
    Start-Process "$InstallDir\run.bat"
    Write-Host "[✓] Агент запущен в фоновом режиме!" -ForegroundColor Green
}
