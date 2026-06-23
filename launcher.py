#!/usr/bin/env python3
"""Swappy one-shot launcher — sets up a venv, installs Swappy, opens a UI.

Usage::

    python launcher.py          # opens the desktop GUI
    python launcher.py tui      # opens the terminal UI instead

Both UIs are dependency-free (standard library only), so this just needs an
editable install of Swappy itself. On Linux the GUI additionally needs the
system Tk package (``sudo apt install python3-tk``); the terminal UI never does.
"""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent
    venv_path = repo_root / "venv"
    ui = "tui" if len(sys.argv) > 1 and sys.argv[1] == "tui" else "gui"

    if not venv_path.exists():
        print("Creating virtual environment…")
        subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True)

    if sys.platform == "win32":
        python_exe = venv_path / "Scripts" / "python.exe"
    else:
        python_exe = venv_path / "bin" / "python"

    print("Installing Swappy (editable)…")
    subprocess.run([str(python_exe), "-m", "pip", "install", "-q", "-e", str(repo_root)],
                   check=False)

    print(f"Starting Swappy {ui.upper()}…")
    return subprocess.run([str(python_exe), "-m", "swap", ui]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
