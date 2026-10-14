@echo off
chcp 65001 >nul
title BadScript Agent Installer for Windows

echo ============================================================
echo           BadScript Agent — Установщик для Windows
echo ============================================================
echo.

set "INSTALL_DIR=%USERPROFILE%\.badscript"
set "REPO_ZIP=https://github.com/Pryanik-M/BadScript/archive/refs/heads/main.zip"
set "VENV_DIR=%INSTALL_DIR%\venv"

:: 1. Проверка наличия Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [X] Python не найден в системе!
    echo     Пожалуйста, скачайте и установите Python 3 с официального сайта:
    echo     https://www.python.org/downloads/
    echo     (ОБЯЗАТЕЛЬНО отметьте галочку "Add python.exe to PATH" при установке)
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version') do set "PY_VER=%%v"
echo [OK] %PY_VER% найден.
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

:: 3. Создание папки установки
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
set "TMP_ZIP=%TEMP%\badscript_main.zip"
set "TMP_EXTRACT=%TEMP%\badscript_extract"

echo [->] Скачивание агента с GitHub...
curl -sL "%REPO_ZIP%" -o "%TMP_ZIP%"
if not exist "%TMP_ZIP%" (
    echo [!] Ошибка скачивания через curl, попытка через PowerShell...
    powershell -NoProfile -Command "Invoke-WebRequest -Uri '%REPO_ZIP%' -OutFile '%TMP_ZIP%'"
)

if not exist "%TMP_ZIP%" (
    echo [X] Не удалось скачать архив агента!
    pause
    exit /b 1
)

echo [OK] Архив успешно скачан.

:: 4. Распаковка архива
echo [->] Распаковка файлов...
if exist "%TMP_EXTRACT%" rmdir /s /q "%TMP_EXTRACT%"
mkdir "%TMP_EXTRACT%"

tar -xf "%TMP_ZIP%" -C "%TMP_EXTRACT%" 2>nul
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Expand-Archive -Path '%TMP_ZIP%' -DestinationPath '%TMP_EXTRACT%' -Force"
)
del /f /q "%TMP_ZIP%" 2>nul

:: Поиск agent.py в распакованной папке
set "AGENT_SRC="
for /d %%D in ("%TMP_EXTRACT%\*") do (
    if exist "%%D\agent\agent.py" (
        set "AGENT_SRC=%%D\agent"
    )
)

if "%AGENT_SRC%"=="" (
    echo [X] Не найден agent.py в скачанном архиве!
    rmdir /s /q "%TMP_EXTRACT%" 2>nul
    pause
    exit /b 1
)

copy /y "%AGENT_SRC%\agent.py" "%INSTALL_DIR%\agent.py" >nul
copy /y "%AGENT_SRC%\shared.py" "%INSTALL_DIR%\shared.py" >nul
if exist "%AGENT_SRC%\requirements.txt" (
    copy /y "%AGENT_SRC%\requirements.txt" "%INSTALL_DIR%\requirements.txt" >nul
)
rmdir /s /q "%TMP_EXTRACT%" 2>nul
echo [OK] Файлы агента скопированы в: %INSTALL_DIR%

:: 5. Создание виртуального окружения и установка библиотек
echo [->] Настройка виртуального окружения venv...
if not exist "%VENV_DIR%" (
    python -m venv "%VENV_DIR%"
)

echo [->] Установка зависимостей (займёт около минуты)...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip -q
"%VENV_DIR%\Scripts\pip.exe" install -q "websockets>=12" "cryptography>=41" "mss>=9" "pillow>=10" "psutil>=5.9" "pynput>=1.7" "numpy>=1.24" "opencv-python>=4.8" "pyautogui>=0.9" "keyboard>=0.13" "sounddevice>=0.4"
echo [OK] Зависимости установлены.

:: 6. Сохранение конфигурации
echo VPS_URL=%VPS_URL%> "%INSTALL_DIR%\config.env"
echo DEVICE_UID=%DEVICE_UID%>> "%INSTALL_DIR%\config.env"
echo DEVICE_SECRET=%DEVICE_SECRET%>> "%INSTALL_DIR%\config.env"

:: 7. Создание лаунчера run.bat
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

echo [OK] Лаунчеры созданы в %INSTALL_DIR%

:: 8. Предложение автозапуска
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
echo Папка:      %INSTALL_DIR%
echo Запуск:     %INSTALL_DIR%\run.bat (фоновый)
echo             %INSTALL_DIR%\run_console.bat (с логами в консоль)
echo.

set /p "RUN_NOW=Запустить агент прямо сейчас? [Y/n]: "
if /i not "%RUN_NOW%"=="n" (
    echo [->] Запуск агента...
    start "" "%INSTALL_DIR%\run.bat"
    echo [OK] Агент запущен в фоновом режиме.
)

echo.
pause
