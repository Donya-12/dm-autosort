@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo Python не найден. Установите его с https://python.org и повторите.
    pause
    exit /b 1
)

python -c "import mutagen" >nul 2>&1
if errorlevel 1 (
    echo Устанавливаю библиотеку mutagen...
    python -m pip install mutagen
    if errorlevel 1 (
        echo Не удалось установить mutagen. Выполните вручную: python -m pip install mutagen
        pause
        exit /b 1
    )
)

set "FOLDER=%~1"
if not defined FOLDER set /p "FOLDER=Папка с музыкой (можно перетащить папку в это окно): "
if not defined FOLDER goto :end

echo.
echo === Предпросмотр ===
python "%~dp0autosort_music.py" "%FOLDER%"
if errorlevel 1 goto :end

echo.
choice /c YN /n /m "Применить переименование? [Y/N] "
if errorlevel 2 goto :end

echo.
echo === Переименование ===
python "%~dp0autosort_music.py" "%FOLDER%" --apply

:end
echo.
pause
