"""Bridge synchronous third-party callbacks to the owning asyncio event loop."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Coroutine
from typing import Any


def call_on_event_loop[Result](
    coroutine: Coroutine[Any, Any, Result], loop: asyncio.AbstractEventLoop
) -> Result:
    """Run one coroutine on its owner loop from a synchronous worker thread."""
    if not loop.is_running() or loop.is_closed():
        coroutine.close()
        raise RuntimeError("Owning event loop is not running")
    try:
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()
    except concurrent.futures.CancelledError as exc:
        raise asyncio.CancelledError from exc
