"""Double-click to start Hoard. Runs without a console window and opens the app window."""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import server
    argv = sys.argv
    server.main(open_window="--no-window" not in argv,
                port=int(argv[argv.index("--port") + 1]) if "--port" in argv else 0)
except Exception:
    log = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "Hoard", "error.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        traceback.print_exc(file=f)
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, f"Hoard couldn't start. Details were written to:\n{log}", "Hoard", 0x10)
