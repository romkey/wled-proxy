"""Ties the inputs, the virtual strip and the outputs together."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time

from . import inputs as inputs_module
from . import protocol
from .config import Config, ConfigError
from .config import load as load_config
from .metrics import RateCounter
from .outputs import Router, describe_coverage
from .pixels import Canvas
from .status import StatusServer

log = logging.getLogger(__name__)


class Proxy:
    """A running proxy: one canvas, its receivers, and its targets.

    Inputs drop pixel data into the canvas as it arrives and flag a frame as
    complete on a DDP push, an Art-Net sync, or the last expected universe.
    The render loop wakes on those flags, coalesces to ``max_fps`` and fans the
    canvas out. Senders that never signal a frame boundary are covered by
    ``frame_timeout_ms``, after which pending data is sent anyway.
    """

    def __init__(self, config: Config):
        self.config = config
        self.canvas = Canvas(config.strip.led_count, config.strip.format)
        self.router = Router.build(self.canvas, config.targets)
        self.inputs: list[inputs_module.InputBase] = []
        self.started_at = time.time()
        self.frames_in = RateCounter()
        self._dirty = False
        self._wake = asyncio.Event()
        self._committed = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self.router.open()
        await self.router.resolve_all()
        await self._start_inputs()
        self._tasks.append(asyncio.create_task(self._render_loop(), name="render"))
        if self.config.output.resolve_interval_s > 0:
            self._tasks.append(asyncio.create_task(self._resolve_loop(), name="resolve"))

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for handler in self.inputs:
            handler.close()
        self.inputs.clear()
        self.router.close()

    async def _start_inputs(self) -> None:
        settings = self.config.inputs
        if settings.ddp.enabled:
            handler = inputs_module.DDPInput(
                settings.ddp, self.canvas, self._on_write, self._on_commit
            )
            await inputs_module.start_input(handler, settings.ddp.bind, settings.ddp.port)
            self.inputs.append(handler)
            log.info("listening for DDP on %s:%d", settings.ddp.bind, settings.ddp.port)

        if settings.artnet.enabled:
            handler = inputs_module.ArtnetInput(
                settings.artnet, self.canvas, self._on_write, self._on_commit
            )
            await inputs_module.start_input(handler, settings.artnet.bind, settings.artnet.port)
            self.inputs.append(handler)
            log.info(
                "listening for Art-Net on %s:%d, universes %d-%d",
                settings.artnet.bind,
                settings.artnet.port,
                settings.artnet.start_universe,
                handler.last_universe,
            )

        if settings.e131.enabled:
            handler = inputs_module.E131Input(
                settings.e131, self.canvas, self._on_write, self._on_commit
            )
            groups = []
            if settings.e131.multicast:
                groups = [
                    protocol.e131_multicast_group(u)
                    for u in range(settings.e131.start_universe, handler.last_universe + 1)
                ]
            await inputs_module.start_input(
                handler,
                settings.e131.bind,
                settings.e131.port,
                groups,
                settings.e131.multicast_interface,
            )
            self.inputs.append(handler)
            log.info(
                "listening for E1.31 on %s:%d, universes %d-%d",
                settings.e131.bind,
                settings.e131.port,
                settings.e131.start_universe,
                handler.last_universe,
            )

        if not self.inputs:
            log.warning("no inputs are enabled, the virtual strip will stay dark")

    # -- input callbacks ---------------------------------------------------

    def _on_write(self) -> None:
        self._dirty = True
        self._wake.set()

    def _on_commit(self) -> None:
        self._committed.set()
        self._wake.set()

    # -- loops -------------------------------------------------------------

    async def _render_loop(self) -> None:
        output = self.config.output
        min_interval = 1.0 / output.max_fps
        frame_timeout = output.frame_timeout_ms / 1000.0
        idle_interval = 1.0 / output.idle_refresh_hz if output.idle_refresh_hz > 0 else None
        last_send = 0.0

        while self._running:
            await _wait(self._wake, idle_interval)
            if not self._running:
                break
            if self._dirty and not self._committed.is_set():
                # Pixels arrived but the sender has not marked the frame as
                # complete yet; wait a little rather than forward half a frame.
                await _wait(self._committed, frame_timeout)
            if not self._dirty and idle_interval is None:
                continue

            delay = min_interval - (time.monotonic() - last_send)
            if delay > 0:
                await asyncio.sleep(delay)
            if not self._running:
                break

            if self._dirty:
                self.frames_in.add()
            self._dirty = False
            self._wake.clear()
            self._committed.clear()
            last_send = time.monotonic()
            self.router.send_frame()

    async def _resolve_loop(self) -> None:
        interval = self.config.output.resolve_interval_s
        while self._running:
            await asyncio.sleep(interval)
            await self.router.resolve_all()

    # -- reporting ---------------------------------------------------------

    def log_summary(self) -> None:
        strip = self.config.strip
        targets = self.config.active_targets
        disabled = len(self.config.targets) - len(targets)
        log.info(
            "virtual strip: %d pixels (%s), %d bytes",
            strip.led_count,
            strip.format.upper(),
            strip.channels,
        )
        log.info(
            "%d target(s) enabled%s, %d packets per frame",
            len(targets),
            f", {disabled} disabled" if disabled else "",
            self.router.packets_per_frame,
        )
        for target in self.router.targets:
            cfg = target.config
            extra = ""
            if cfg.protocol in ("artnet", "e131"):
                last = cfg.universe + target.packets_per_frame - 1
                extra = f", universes {cfg.universe}-{last}"
            log.info(
                "  %-20s %5d-%-5d -> %s:%d %s/%s%s%s",
                cfg.name,
                cfg.start,
                cfg.end - 1,
                cfg.host,
                cfg.port,
                cfg.protocol,
                cfg.format,
                extra,
                " (reversed)" if cfg.reverse else "",
            )

        gaps, overlaps = describe_coverage(strip.led_count, targets)
        if gaps:
            log.warning("virtual strip pixels with no target: %s", format_runs(gaps))
        if overlaps:
            log.info("pixels sent to more than one target: %s", format_runs(overlaps))

    def snapshot(self) -> dict:
        """The status document served over HTTP."""
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "config": self.config.source,
            "virtual_strip": {
                "led_count": self.config.strip.led_count,
                "format": self.config.strip.format,
            },
            "input": {
                "frames_per_second": round(self.frames_in.rate, 1),
                "sources": [
                    {
                        "protocol": handler.protocol_name,
                        "packets_per_second": round(handler.packets.rate, 1),
                        "packets": handler.packets.total,
                        "frames": handler.frames.total,
                        "rejected": handler.rejected,
                        "last_source": handler.last_source,
                    }
                    for handler in self.inputs
                ],
            },
            "output": {
                "frames_per_second": round(self.router.frames.rate, 1),
                "packets_per_frame": self.router.packets_per_frame,
                "targets": [
                    {
                        "name": t.name,
                        "host": t.config.host,
                        "address": t.resolved_host,
                        "port": t.config.port,
                        "protocol": t.config.protocol,
                        "format": t.config.format,
                        "start": t.config.start,
                        "count": t.config.count,
                        "reverse": t.config.reverse,
                        "frames_per_second": round(t.frames.rate, 1),
                        "packets": t.packets.total,
                        "bytes": t.bytes_sent,
                        "errors": t.errors,
                        "last_error": t.last_error,
                    }
                    for t in self.router.targets
                ],
            },
        }


async def _wait(event: asyncio.Event, timeout: float | None) -> None:
    """Wait for an event, giving up after ``timeout`` seconds if given."""
    if event.is_set():
        return
    if timeout is None:
        await event.wait()
        return
    with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
        await asyncio.wait_for(event.wait(), timeout)


def format_runs(runs: list[tuple[int, int]], limit: int = 6) -> str:
    shown = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in runs[:limit])
    if len(runs) > limit:
        shown += f", and {len(runs) - limit} more"
    return shown


async def run(config: Config) -> int:
    """Run until a signal asks us to stop. SIGHUP reloads the config file."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    reload_requested = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    with contextlib.suppress(AttributeError, NotImplementedError, ValueError):
        loop.add_signal_handler(signal.SIGHUP, reload_requested.set)

    status: StatusServer | None = None
    proxy = Proxy(config)
    await proxy.start()
    proxy.log_summary()

    if config.status.enabled:
        status = StatusServer(config.status.bind, config.status.port, lambda: proxy.snapshot())
        await status.start()
        log.info("status page on http://%s:%d/", config.status.bind, config.status.port)

    try:
        while True:
            waiters = [
                asyncio.create_task(stop.wait()),
                asyncio.create_task(reload_requested.wait()),
            ]
            _, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stop.is_set():
                break

            reload_requested.clear()
            log.info("reloading %s", config.source)
            try:
                new_config = load_config(config.source)
            except ConfigError as exc:
                log.error("reload failed, keeping the running configuration: %s", exc)
                continue
            await proxy.stop()
            config = new_config
            logging.getLogger("wled_proxy").setLevel(config.log_level.upper())
            proxy = Proxy(config)
            await proxy.start()
            proxy.log_summary()
    finally:
        log.info("shutting down")
        if status is not None:
            await status.stop()
        await proxy.stop()
    return 0
