@echo off
REM Swappy desktop GUI launcher for Windows.
REM Double-click this, or run it from a terminal, to open the GUI.

if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat

echo Installing Swappy...
pip install -q -e .

echo Starting Swappy GUI...
python -m swap gui
pause
