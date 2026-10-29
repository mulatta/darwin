"""Hold one marimo editor session open without a browser.

PAIR_WS_URL=<ws-url> python _hold.py

A headless marimo server only starts a kernel for a connected client; this
process is that client and exits when the server closes the connection.
"""

from __future__ import annotations

import asyncio
import os

import websockets


async def main(url: str) -> None:
    async with websockets.connect(url, max_size=None) as ws:
        async for _ in ws:
            pass


if __name__ == "__main__":
    asyncio.run(main(os.environ["PAIR_WS_URL"]))
