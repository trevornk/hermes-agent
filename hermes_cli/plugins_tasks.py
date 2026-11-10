"""Profile binding for supervised plugin background work."""

import asyncio
import contextvars
import logging
import threading
from concurrent.futures import Future
from pathlib import Path


async def run_profile_task(coro, profile_home: str):
    """Bind the manager's home, never the turn that happened to spawn the task."""
    from agent.secret_scope import (
        reset_secret_scope, set_secret_scope,
    )
    from hermes_constants import (
        reset_hermes_home_override, set_hermes_home_override,
    )

    # Registration can precede multiplex activation; bind ownership for the entire task.
    home_token = set_hermes_home_override(profile_home)
    secret_token = None
    started = False
    try:
        from gateway.run import _load_profile_secret_scope

        secrets = await asyncio.to_thread(_load_profile_secret_scope, Path(profile_home))
        secret_token = set_secret_scope(secrets, profile_home=profile_home)
        started = True
        return await coro
    finally:
        # Cancellation or a failed load can happen before the plugin coroutine starts.
        if not started:
            coro.close()
        if secret_token is not None:
            reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


class ThreadTask(Future):
    """Concurrent future for a daemon-loop task; ``thread`` allows waiting for shutdown.

    Cancellation requests loop-thread cancellation. As with a concurrent future,
    ``done()`` becomes true on cancellation before coroutine cleanup finishes.
    """

    def __init__(self, coro, profile_home, name, on_exit):
        super().__init__()
        self._loop_lock = threading.RLock()
        self._loop = None
        self._task = None
        self._cancel_requested = False
        context = contextvars.copy_context()
        self.thread = threading.Thread(
            target=context.run, args=(self._run, coro, profile_home, on_exit),
            name=name, daemon=True,
        )

    def cancel(self):
        # Schedule before Future.cancel invokes user callbacks, without holding
        # the loop lock while those callbacks run.
        with self._loop_lock:
            if not self.done():
                self._cancel_requested = True
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._task.cancel)
        return super().cancel()

    async def _run_bound(self, coro, profile_home):
        with self._loop_lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            if self._cancel_requested:
                self._task.cancel()
        try:
            return await run_profile_task(coro, profile_home)
        finally:
            # Clear before asyncio.run closes the loop, racing cancellation safely.
            with self._loop_lock:
                self._loop = self._task = None

    def _run(self, coro, profile_home, on_exit):
        logger = logging.getLogger("hermes_cli.plugins")

        try:
            result = asyncio.run(self._run_bound(coro, profile_home))
        except asyncio.CancelledError:
            self.cancel()
        except BaseException as exc:
            logger.exception("Plugin background task %s failed", self.thread.name)
            if self.set_running_or_notify_cancel():
                self.set_exception(exc)
        else:
            if self.set_running_or_notify_cancel():
                self.set_result(result)
        finally:
            coro.close()
            on_exit()


def spawn_profile_task(ctx, coro, *, name=None):
    """Create and supervise a task owned by this plugin manager."""
    from hermes_cli.plugins import logger

    if not asyncio.iscoroutine(coro):
        raise TypeError("spawn_task expects a coroutine")
    task_name = name or f"plugin:{ctx.plugin_id}:task"
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        task = ThreadTask(coro, ctx._manager.scope_key, task_name, lambda: handle.dispose())
        handle = ctx._track("background_task", task_name, lambda: task.done() or task.cancel())
        task.thread.start()
        return task
    task = loop.create_task(run_profile_task(coro, ctx._manager.scope_key), name=task_name)
    handle = ctx._track("background_task", task_name, lambda: task.done() or task.cancel())
    def on_done(_task):
        # The wrapper may be cancelled before it ever awaits the plugin coroutine.
        coro.close()
        handle.dispose()

    task.add_done_callback(on_done)
    logger.debug("Plugin %s spawned supervised task: %s", ctx.manifest.name, task_name)
    return task
