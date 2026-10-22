"""Separately runnable worker entry point; domain and adapters are shared with API."""

from harnessa.services.worker import run
import asyncio

if __name__ == "__main__":
    asyncio.run(run())
