"""Double-click to start ClipForge with no terminal window. Opens it in your browser when ready.
If ClipForge is already running, this only opens the browser. Log: data\\server.log"""
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
URL = "http://localhost:8000"


def running() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/", timeout=1.5)
        return True
    except urllib.error.HTTPError:
        return True                      # it answered, so it is running
    except Exception:
        return False


def open_when_ready() -> None:
    for _ in range(120):
        if running():
            break
        time.sleep(1)
    webbrowser.open(URL)


def main() -> None:
    quiet = "--no-browser" in sys.argv          # used when Windows starts ClipForge at login
    if running():
        if not quiet:
            webbrowser.open(URL)
        return
    venv = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")
    if os.path.exists(venv) and os.path.normcase(sys.executable) != os.path.normcase(venv):
        subprocess.Popen([venv, os.path.abspath(__file__)] + sys.argv[1:], cwd=ROOT)   # hand over to ClipForge's own Python
        return
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    log = open(os.path.join(ROOT, "data", "server.log"), "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = log                                          # no console exists under pythonw
    with open(os.path.join(ROOT, "data", "clipforge.pid"), "w") as f:
        f.write(str(os.getpid()))
    backend = os.path.join(ROOT, "backend")
    os.chdir(backend)
    sys.path.insert(0, backend)
    if not quiet:
        threading.Thread(target=open_when_ready, daemon=True).start()
    import uvicorn
    from app import config
    host = "0.0.0.0" if config.PHONE_ACCESS else "127.0.0.1"
    if config.PHONE_ACCESS:
        print(f"Phone access is on: http://{config.lan_ip() or '<no network>'}:8000")
    uvicorn.run("app.main:app", host=host, port=8000)


if __name__ == "__main__":
    main()
