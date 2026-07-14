"""Entry point for the packaged .exe (PyInstaller).

Starts the FastAPI app on a local port and opens the default browser. Not
used during normal `uvicorn main:app --reload` development - that's the dev
loop. This script is what `pyinstaller ... backend/launcher.py` bundles into
the standalone executable (see the README's packaging section).
"""
from __future__ import annotations

import socket
import threading
import time
import webbrowser

import uvicorn

from main import app

HOST = "127.0.0.1"


def find_free_port(start: int = 8420, tries: int = 20) -> int:
    """Find the first free TCP port at or after ``start``.

    Args:
        start: The first port number to try.
        tries: How many consecutive ports to check before giving up.

    Returns:
        The first free port found.

    Raises:
        RuntimeError: If no free port is found within ``tries`` attempts.
    """
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex((HOST, port)) != 0:
                return port
    raise RuntimeError("No free port found for OpsPilot server")


def open_browser_when_ready(url: str, port: int, timeout: float = 10.0) -> None:
    """Poll the server port and open the browser once it accepts connections.

    Runs in a background thread so the main thread can start uvicorn
    immediately without a race between "server is listening" and "browser
    tab opens".

    Args:
        url: The URL to open once the server is ready.
        port: The port to poll for a listening socket.
        timeout: Give up polling after this many seconds.
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((HOST, port), timeout=0.5):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.25)


PORT = find_free_port()

if __name__ == "__main__":
    server_url = f"http://{HOST}:{PORT}"
    threading.Thread(
        target=open_browser_when_ready, args=(server_url, PORT), daemon=True
    ).start()
    print(f"OpsPilot running at {server_url} - close the browser tab to stop the server.")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
