"""Run on the target OS. The wheel and all tests are built separately in Docker."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
subprocess.run([
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
    "--onedir", "--name", "AirFrame", "--collect-all", "aiortc", "--collect-all", "av",
    "--collect-data", "airframe",
    str(root / "scripts" / "desktop-entry.py"),
], cwd=root, check=True)
