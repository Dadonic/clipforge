"""Double-click to stop ClipForge when it was started with "Start ClipForge"."""
import os
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
PID = os.path.join(ROOT, "data", "clipforge.pid")
if os.path.exists(PID):
    try:
        pid = int(open(PID).read().strip())
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    except (ValueError, OSError):
        pass
    try:
        os.remove(PID)
    except OSError:
        pass
