"""ANAF Intelligence Platform v2 — Entry Point
Run: python main.py
"""
import sys, os, threading, time, webbrowser
sys.path.insert(0, os.path.dirname(__file__))

from config import Config
from backend.utils.logger import get_logger

log = get_logger(__name__)


def open_browser(url: str, delay: float = 1.8):
    def _go():
        time.sleep(delay)
        webbrowser.open(url)
    threading.Thread(target=_go, daemon=True).start()


def main():
    import uvicorn
    from backend.api.routes import app

    # Windows consoles often default to cp1252, which can't encode the
    # box-drawing characters in the startup banner below — force UTF-8.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # Ensure data directory exists
    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    url = f"http://{Config.HOST}:{Config.PORT}"
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         ANAF Intelligence Platform  v2.0                    ║
║         Romanian Company Financial Analytics                 ║
╠══════════════════════════════════════════════════════════════╣
║  Local URL  : {url:<46}║
║  Press Ctrl+C to stop                                        ║
╚══════════════════════════════════════════════════════════════╝
""")
    open_browser(url)
    uvicorn.run(app, host=Config.HOST, port=Config.PORT, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
