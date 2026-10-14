@echo off
chcp 65001 >nul
title BadScript Agent Installer for Windows

echo ============================================================
echo           BadScript Agent — Установщик для Windows
echo ============================================================
echo.

set "INSTALL_DIR=%USERPROFILE%\.badscript"
set "RAW_BASE=https://raw.githubusercontent.com/Pryanik-M/BadScript/main/agent"
set "VENV_DIR=%INSTALL_DIR%\venv"

:: 1. Строгая проверка наличия рабочего Python 3
set "PY_BIN="

:: Проверяем стандартный python
python -c "import sys; assert sys.version_info[0] >= 3; print('Python ' + sys.version.split()[0])" > "%TEMP%\py_check.txt" 2>nul
if %errorlevel% equ 0 (
    set "PY_BIN=python"
    set /p PY_VER=<"%TEMP%\py_check.txt"
    del /f /q "%TEMP%\py_check.txt" 2>nul
    goto :python_found
)

:: Проверяем py launcher
py -3 -c "import sys; assert sys.version_info[0] >= 3; print('Python ' + sys.version.split()[0])" > "%TEMP%\py_check.txt" 2>nul
if %errorlevel% equ 0 (
    set "PY_BIN=py -3"
    set /p PY_VER=<"%TEMP%\py_check.txt"
    del /f /q "%TEMP%\py_check.txt" 2>nul
    goto :python_found
)

del /f /q "%TEMP%\py_check.txt" 2>nul

echo [X] ОШИБКА: Python 3 не найден или не установлен!
echo.
echo     Для работы агента требуется Python 3.8 или выше:
echo     1. Скачайте Python с официального сайта: https://www.python.org/downloads/
echo     2. При установке ОБЯЗАТЕЛЬНО поставьте галочку "Add python.exe to PATH"
echo     3. После установки перезапустите этот установщик.
echo.
pause
exit /b 1

:python_found
echo [OK] %PY_VER% обнаружен (%PY_BIN%).
echo.

:: 2. Ввод данных подключения
echo Введите данные для подключения агента:
echo.

set /p "VPS_URL=  VPS адрес панели (например ws://192.168.31.100:8000/ws/agent/): "
if "%VPS_URL%"=="" (
    echo [X] Адрес VPS не может быть пустым!
    pause
    exit /b 1
)

for /f "delims=" %%u in ('%PY_BIN% -c "import urllib.parse; u='%VPS_URL%'.strip(); u='ws://'+u[7:] if u.startswith('http://') else ('wss://'+u[8:] if u.startswith('https://') else (u if u.startswith(('ws://','wss://')) else 'ws://'+u)); p=urllib.parse.urlparse(u); u=u.rstrip('/')+'/ws/agent/' if not p.path or p.path=='/' else (u if u.endswith('/') else u+'/'); print(u)"') do set "VPS_URL=%%u"

set /p "DEVICE_UID=  UID устройства (из панели): "
if "%DEVICE_UID%"=="" (
    echo [X] UID не может быть пустым!
    pause
    exit /b 1
)

set /p "DEVICE_SECRET=  Секретный токен (из панели): "
if "%DEVICE_SECRET%"=="" (
    echo [X] Токен не может быть пустым!
    pause
    exit /b 1
)

echo.
echo VPS:    %VPS_URL%
echo UID:    %DEVICE_UID%
echo Secret: ********
echo.

:: 3. Создание папки установки и скачивание ТОЛЬКО файлов агента
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

echo [->] Скачивание файлов агента с GitHub (только агент, без веб-панели)...
curl -sL "%RAW_BASE%/agent.py" -o "%INSTALL_DIR%\agent.py"
curl -sL "%RAW_BASE%/shared.py" -o "%INSTALL_DIR%\shared.py"
curl -sL "%RAW_BASE%/requirements.txt" -o "%INSTALL_DIR%\requirements.txt"

if not exist "%INSTALL_DIR%\agent.py" (
    echo [!] Ошибка скачивания через curl, попытка через PowerShell...
    powershell -NoProfile -Command "Invoke-WebRequest -Uri '%RAW_BASE%/agent.py' -OutFile '%INSTALL_DIR%\agent.py'; Invoke-WebRequest -Uri '%RAW_BASE%/shared.py' -OutFile '%INSTALL_DIR%\shared.py'; Invoke-WebRequest -Uri '%RAW_BASE%/requirements.txt' -OutFile '%INSTALL_DIR%\requirements.txt'"
)

if not exist "%INSTALL_DIR%\agent.py" (
    echo [X] Ошибка: Не удалось скачать agent.py! Проверьте интернет-соединение.
    pause
    exit /b 1
)

echo [OK] Файлы агента скачаны в: %INSTALL_DIR%

:: 4. Создание виртуального окружения
echo [->] Настройка виртуального окружения Python (venv)...
if not exist "%VENV_DIR%\Scripts\python.exe" (
    %PY_BIN% -m venv "%VENV_DIR%"
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [X] Ошибка: Не удалось создать виртуальное окружение venv!
    pause
    exit /b 1
)

echo [->] Установка библиотек агента...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip -q
"%VENV_DIR%\Scripts\pip.exe" install -q "websockets>=12" "cryptography>=41" "mss>=9" "pillow>=10" "psutil>=5.9" "pynput>=1.7" "numpy>=1.24" "opencv-python>=4.8" "pyautogui>=0.9" "keyboard>=0.13" "sounddevice>=0.4"

if %errorlevel% neq 0 (
    echo [X] Внимание: возникли ошибки при установке некоторых библиотек.
) else (
    echo [OK] Библиотеки успешно установлены.
)

:: 5. Сохранение конфигурации
echo VPS_URL=%VPS_URL%> "%INSTALL_DIR%\config.env"
echo DEVICE_UID=%DEVICE_UID%>> "%INSTALL_DIR%\config.env"
echo DEVICE_SECRET=%DEVICE_SECRET%>> "%INSTALL_DIR%\config.env"

:: 6. Создание лаунчеров
(
echo @echo off
echo cd /d "%INSTALL_DIR%"
echo for /f "usebackq tokens=1,* delims==" %%%%A in ("config.env"^) do set "%%%%A=%%%%B"
echo start "" "%VENV_DIR%\Scripts\pythonw.exe" "%INSTALL_DIR%\agent.py" --vps "%%VPS_URL%%" --uid "%%DEVICE_UID%%" --secret "%%DEVICE_SECRET%%" --watchdog --hidden
) > "%INSTALL_DIR%\run.bat"

(
echo @echo off
echo cd /d "%INSTALL_DIR%"
echo for /f "usebackq tokens=1,* delims==" %%%%A in ("config.env"^) do set "%%%%A=%%%%B"
echo "%VENV_DIR%\Scripts\python.exe" "%INSTALL_DIR%\agent.py" --vps "%%VPS_URL%%" --uid "%%DEVICE_UID%%" --secret "%%DEVICE_SECRET%%" --watchdog
) > "%INSTALL_DIR%\run_console.bat"

echo [OK] Лаунчеры созданы:
echo      %INSTALL_DIR%\run.bat (фоновый)
echo      %INSTALL_DIR%\run_console.bat (консольный)

:: 7. Автозапуск
echo.
set /p "AUTOSTART_CHOICE=Установить автозапуск при включении компьютера? [Y/n]: "
if /i not "%AUTOSTART_CHOICE%"=="n" (
    powershell -NoProfile -Command "Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'BadScriptAgent' -Value '\"%INSTALL_DIR%\run.bat\"'"
    echo [OK] Автозапуск добавлен в реестр Windows (HKCU Run).
)

echo.
echo ============================================================
echo            Установка успешно завершена! [OK]
echo ============================================================
echo.

set /p "RUN_NOW=Запустить агент прямо сейчас? [Y/n]: "
if /i not "%RUN_NOW%"=="n" (
    echo [->] Запуск агента...
    start "" "%INSTALL_DIR%\run.bat"
    echo [OK] Агент запущен в фоновом режиме.
)

echo.
pause
