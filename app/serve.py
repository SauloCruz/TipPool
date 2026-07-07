"""Entrypoint: `python -m app.serve` — the single process behind `make run`.
Binds HOST:PORT from .env and prints the LAN URL for tablets/phones."""

from __future__ import annotations

import socket

import uvicorn

from .config import Settings
from .main import create_app


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # no packets sent; just picks a route
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def main() -> None:
    settings = Settings()
    app = create_app(settings)
    shown_host = lan_ip() if settings.host in ("0.0.0.0", "::") else settings.host
    print(f"\n  Tavern Law Tip Pool")
    print(f"  On this machine:  http://127.0.0.1:{settings.port}")
    print(f"  On venue Wi-Fi:   http://{shown_host}:{settings.port}")
    print(f"  Database:         {settings.db_path}\n")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
