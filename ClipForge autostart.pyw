"""Copy this file into the Windows Startup folder (Windows + R, then type shell:startup) and ClipForge
starts quietly every time you log in. It does not open the browser. Delete the copy to stop that."""
import os
import subprocess
import sys

START = os.path.join(os.path.expanduser("~"), "Documents", "clipforge-main", "Start ClipForge.pyw")
if os.path.exists(START):
    subprocess.Popen([sys.executable, START, "--no-browser"], cwd=os.path.dirname(START))
