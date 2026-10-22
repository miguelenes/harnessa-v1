"""Separately runnable worker entry point; domain and adapters are shared with API."""

import asyncio

from harnessa.services.worker import run

if __name__ == "__main__":
    asyncio.run(run())
