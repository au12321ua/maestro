"""Bidirectional environment permits. Losing the channel forbids new starts."""

import asyncio
import json
from contextlib import asynccontextmanager

from maestro_runner.protocol import emit


async def _complete_before_cancelling(task, on_cancel=None):
    """Delay cancellation until the operation and its CLI commands have returned.

    Cancelling Harbor's wait can orphan a Compose command that still creates
    containers. Keep it owned and keep the permit held while it drains. A stuck
    operation remains within the Runner group for the service's forced stop.
    """
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if on_cancel is not None:
                on_cancel()
            if task.done():
                if not task.cancelled():
                    task.exception()
                raise cancellation or error
            cancellation = cancellation or error
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise
        else:
            if cancellation is not None:
                raise cancellation
            return result


class Permits:
    def __init__(self, attempt_id):
        self.attempt_id = attempt_id
        self.waiters = {}
        self.held = {}
        self.retired = set()
        self.closed = False

    async def acquire(self, phase):
        if self.closed:
            raise RuntimeError("resource channel is closed")
        if phase in self.retired:
            raise RuntimeError("environment stage has already been released")
        if phase in self.held:
            return self.held[phase]
        key = (phase, "permit_granted")
        future = self.waiters.get(key)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self.waiters[key] = future
            emit("resource_requested", attempt_id=self.attempt_id, phase=phase)
        permit = await asyncio.shield(future)
        if self.closed:
            raise RuntimeError("resource channel is closed")
        if phase in self.retired:
            raise RuntimeError("environment stage has already been released")
        self.held[phase] = permit
        return permit

    async def release(self, phase):
        if phase not in self.held or self.closed:
            return
        self.retired.add(phase)
        key = (phase, "permit_released")
        future = self.waiters.get(key)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self.waiters[key] = future
            emit(
                "resource_released",
                attempt_id=self.attempt_id,
                phase=phase,
                permit_id=self.held[phase],
            )
        await asyncio.shield(future)
        self.held.pop(phase, None)

    def accept(self, command):
        if command.get("attempt_id") != self.attempt_id:
            raise ValueError("command for another execution attempt")
        phase, kind = command.get("phase"), command.get("command")
        future = self.waiters.get((phase, kind))
        permit = command.get("permit_id")
        if future is None or not isinstance(permit, str) or not permit:
            raise ValueError("unexpected resource permission")
        if future.done():
            if future.result() != permit:
                raise ValueError("conflicting resource permission")
            return
        if kind == "permit_released" and self.held.get(phase) != permit:
            raise ValueError("release for another permit")
        future.set_result(permit)

    def close(self):
        self.closed = True
        for future in self.waiters.values():
            if not future.done():
                future.cancel()

    def wrap_environment(self, environment, phase):
        start, stop = environment.start, environment.stop
        starting = set()
        shutdown = None

        async def start_permitted(*args, **kwargs):
            await self.acquire(phase)
            task = asyncio.create_task(start(*args, **kwargs))
            starting.add(task)
            try:
                return await _complete_before_cancelling(task, lambda: self.retired.add(phase))
            finally:
                starting.discard(task)

        async def stop_after_start(*args, **kwargs):
            # A container list is only a release proof once no outstanding start
            # can create another container. Drain starts before invoking Compose down.
            await asyncio.gather(*starting, return_exceptions=True)
            # Upstream can swallow stop failures. The platform independently sweeps
            # the labelled group before acknowledging release, even after an error.
            try:
                return await stop(*args, **kwargs)
            finally:
                await self.release(phase)

        async def stop_confirmed(*args, **kwargs):
            nonlocal shutdown
            self.retired.add(phase)
            if shutdown is None:
                shutdown = asyncio.create_task(stop_after_start(*args, **kwargs))
            return await _complete_before_cancelling(shutdown)

        environment.start = start_permitted
        environment.stop = stop_confirmed
        return environment

    def install_verifier_gate(self):
        from harbor.trial.trial import Trial

        original = Trial._separate_verifier_env
        permits = self

        @asynccontextmanager
        async def permitted(trial, *args, **kwargs):
            # Capacity waiting is outside Harbor's environment-build timeout.
            await permits.acquire("verification")
            async with original(trial, *args, **kwargs) as environment:
                yield environment

        Trial._separate_verifier_env = permitted


async def consume(stream, permits, cancel):
    lost = True
    try:
        while line := await stream.readline():
            command = json.loads(line)
            if command.get("command") == "stop":
                break
            permits.accept(command)
    except asyncio.CancelledError:
        lost = False
        raise
    except (ValueError, KeyError):
        pass
    finally:
        permits.close()
        if lost:
            cancel()
