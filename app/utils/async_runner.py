import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

_thread_local = threading.local()


def get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    """Get or create a persistent event loop for the current thread."""
    loop = getattr(_thread_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _thread_local.loop = loop
    return loop


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Execute an asynchronous coroutine from a synchronous context safely.

    Maintains a persistent thread-local event loop for Celery workers to prevent
    connection pool and client binding conflicts across asynchronous drivers.
    If called from within an active event loop (e.g., in test suites), executes
    via an isolated thread executor.
    """
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if running_loop is not None and running_loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result()

    loop = get_or_create_event_loop()
    return loop.run_until_complete(coro)
