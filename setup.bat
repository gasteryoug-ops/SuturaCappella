@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo.
echo =====================================
echo    SuturaCappella - Setup
echo =====================================
echo.

:: Vérifier Python
echo [1/4] Vérification de Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ Python non trouvé!
    echo.
    echo Télécharge Python: https://www.python.org/downloads/
    echo Assure-toi d'ajouter Python au PATH pendant l'installation.
    echo.
    pause
    exit /b 1
)
echo ✓ Python trouvé

:: Vérifier ffmpeg
echo.
echo [2/4] Vérification de ffmpeg...
ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ ffmpeg non trouvé!
    echo.
    echo Options:
    echo 1. Télécharge ffmpeg: https://ffmpeg.org/download.html
    echo 2. Ou via Chocolatey: choco install ffmpeg
    echo.
    echo Après installation, redémarre ce script.
    echo.
    pause
    exit /b 1
)
echo ✓ ffmpeg trouvé

:: Installer packages Python
echo.
echo [3/4] Installation des packages Python...
pip install customtkinter tkinterdnd2 pillow opencv-python numpy

:: PyTorch optionnel
echo.
echo [4/4] PyTorch GPU (optionnel)
echo Voulez-vous installer PyTorch CUDA pour accélération GPU?
echo (Recommandé si vous avez une NVIDIA GPU)
echo.
set /p install_torch="Installer PyTorch? (y/n): "

if /i "%install_torch%"=="y" (
    echo Installation de PyTorch CUDA...
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
    echo ✓ PyTorch CUDA installé
) else (
    echo PyTorch non installé. SuturaCappella fonctionnera en CPU.
)

echo.
echo =====================================
echo ✓ Installation réussie!
echo =====================================
echo.
echo Tu peux maintenant lancer:
echo python DETX_Rhythmo_Generator_V4_Optimized.py
echo.
pause
