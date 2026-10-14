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
$RepoZip = "https://github.com/Pryanik-M/BadScript/archive/refs/heads/main.zip"
$VenvDir = "$InstallDir\venv"

# 1. Проверка Python
try {
    $pyVer = & python --version 2>&1
    Write-Host "[✓] $pyVer найден" -ForegroundColor Green
} catch {
    Write-Host "[✗] Python не найден в системе!" -ForegroundColor Red
    Write-Host "    Установите Python 3 с https://www.python.org/downloads/ (отметьте Add to PATH)"
    Read-Host "Нажмите Enter для выхода..."
    exit 1
}

# 2. Ввод данных подключения
Write-Host ""
Write-Host "Введите данные для подключения агента:" -ForegroundColor Yellow
$VpsUrl = Read-Host "  VPS адрес панели (например ws://192.168.31.100:8000/ws/agent/)"
if ([string]::IsNullOrWhiteSpace($VpsUrl)) {
    Write-Host "[✗] Адрес VPS не может быть пустым!" -ForegroundColor Red
    exit 1
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

# 3. Скачивание
if (!(Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}

$TmpZip = "$env:TEMP\badscript_agent.zip"
$TmpExtract = "$env:TEMP\badscript_extract"

Write-Host ""
Write-Host "[→] Скачивание агента с GitHub..." -ForegroundColor Cyan
Invoke-WebRequest -Uri $RepoZip -OutFile $TmpZip

if (Test-Path $TmpExtract) {
    Remove-Item $TmpExtract -Recurse -Force
}
Expand-Archive -Path $TmpZip -DestinationPath $TmpExtract -Force
Remove-Item $TmpZip -Force

# Поиск файлов
$AgentFolder = Get-ChildItem -Path $TmpExtract -Recurse -Filter "agent.py" | Select-Object -First 1
if (!$AgentFolder) {
    Write-Host "[✗] agent.py не найден в архиве!" -ForegroundColor Red
    exit 1
}

$SrcDir = $AgentFolder.DirectoryName
Copy-Item "$SrcDir\agent.py" "$InstallDir\agent.py" -Force
Copy-Item "$SrcDir\shared.py" "$InstallDir\shared.py" -Force
if (Test-Path "$SrcDir\requirements.txt") {
    Copy-Item "$SrcDir\requirements.txt" "$InstallDir\requirements.txt" -Force
}
Remove-Item $TmpExtract -Recurse -Force

Write-Host "[✓] Файлы агента скопированы в: $InstallDir" -ForegroundColor Green

# 4. Виртуальное окружение
Write-Host "[→] Настройка виртуального окружения Python..." -ForegroundColor Cyan
if (!(Test-Path $VenvDir)) {
    & python -m venv $VenvDir
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
for /f "usebackq tokens=1,* delims==" %%A in ("config.env") do set "%%A=%%B"
start "" "$VenvDir\Scripts\pythonw.exe" "$InstallDir\agent.py" --vps "%VPS_URL%" --uid "%DEVICE_UID%" --secret "%DEVICE_SECRET%" --watchdog --hidden
"@
$RunBat | Out-File -FilePath "$InstallDir\run.bat" -Encoding ascii

$RunConsoleBat = @"
@echo off
cd /d "$InstallDir"
for /f "usebackq tokens=1,* delims==" %%A in ("config.env") do set "%%A=%%B"
"$VenvDir\Scripts\python.exe" "$InstallDir\agent.py" --vps "%VPS_URL%" --uid "%DEVICE_UID%" --secret "%DEVICE_SECRET%" --watchdog
"@
$RunConsoleBat | Out-File -FilePath "$InstallDir\run_console.bat" -Encoding ascii

# 7. Автозапуск
$Autostart = Read-Host "Установить автозапуск при входе в систему? [Y/n]"
if ($Autostart -ne "n" -and $Autostart -ne "N") {
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "BadScriptAgent" -Value "`"$InstallDir\run.bat`""
    Write-Host "[✓] Автозапуск добавлен в реестр" -ForegroundColor Green
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
