"""Output targets: the real WLED devices the virtual strip is fanned out to."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import time
from collections.abc import Iterator

from . import protocol
from .config import TargetConfig
from .metrics import RateCounter
from .pixels import Canvas, PixelMapper

log = logging.getLogger(__name__)

SEND_BUFFER_BYTES = 1 << 20
ERROR_LOG_INTERVAL = 30.0


class Target:
    """One downstream device, owning its socket, mapping and counters."""

    def __init__(self, config: TargetConfig, canvas: Canvas):
        self.config = config
        self.name = config.name
        self.channels_per_pixel = config.channels_per_pixel
        self.mapper = PixelMapper(
            source_format=canvas.format,
            start=config.start,
            count=config.count,
            reverse=config.reverse,
            target_format=config.format,
            color_order=config.color_order,
            white_mode=config.white_mode,
            white_merge=config.white_merge,
            brightness=config.brightness,
            gamma=config.gamma,
        )
        self.address: tuple[str, int] | None = None
        self.resolved_host: str | None = None
        self.frames = RateCounter()
        self.packets = RateCounter()
        self.bytes_sent = 0
        self.errors = 0
        self.last_error: str | None = None
        self._error_logged_at = 0.0
        self._sock: socket.socket | None = None
        self._sequence = 0
        self._universe_sequences: dict[int, int] = {}
        self._cid = _stable_cid(config.name)
        self._source_name = config.source_name.encode("utf-8")[:63].ljust(64, b"\x00")

    @property
    def packets_per_frame(self) -> int:
        payload = self.config.count * self.channels_per_pixel
        chunk = (
            protocol.DDP_MAX_DATA_LEN
            if self.config.protocol == "ddp"
            else self.config.channels_per_universe
        )
        return max(1, -(-payload // chunk))

    def open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER_BYTES)
        except OSError:
            pass
        if self.config.multicast:
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.config.multicast_ttl
            )
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    async def resolve(self) -> bool:
        """Look up the target's host name. Multicast targets need no lookup."""
        if self.config.multicast and self.config.protocol == "e131":
            self.address = ("", self.config.port)
            self.resolved_host = "multicast"
            return True
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                self.config.host,
                self.config.port,
                family=socket.AF_INET,
                type=socket.SOCK_DGRAM,
            )
        except OSError as exc:
            self._note_error(f"cannot resolve {self.config.host}: {exc}")
            return False
        address = infos[0][4]
        if address != self.address:
            log.info("target %s resolves to %s", self.name, address[0])
        self.address = (address[0], address[1])
        self.resolved_host = address[0]
        return True

    def send(self, view: memoryview) -> None:
        if self._sock is None or self.address is None:
            return
        payload = self.mapper.render(view)
        sent = 0
        for destination, packet in self._build(payload):
            if self._send_one(packet, destination):
                sent += 1
        if sent:
            self.frames.add()
            self.packets.add(sent)

    def _send_one(self, packet: bytes, destination: tuple[str, int]) -> bool:
        try:
            self._sock.sendto(packet, destination)
        except BlockingIOError:
            self._note_error("send buffer full")
            return False
        except OSError as exc:
            self._note_error(str(exc))
            return False
        self.bytes_sent += len(packet)
        return True

    def _note_error(self, message: str) -> None:
        self.errors += 1
        self.last_error = message
        now = time.monotonic()
        if now - self._error_logged_at >= ERROR_LOG_INTERVAL:
            self._error_logged_at = now
            log.warning("target %s: %s", self.name, message)

    def _build(self, payload) -> Iterator[tuple[tuple[str, int], bytes]]:
        if self.config.protocol == "ddp":
            return self._build_ddp(payload)
        if self.config.protocol == "artnet":
            return self._build_artnet(payload)
        return self._build_e131(payload)

    def _build_ddp(self, payload) -> Iterator[tuple[tuple[str, int], bytes]]:
        rgbw = self.channels_per_pixel == 4
        base = self.config.remote_start * self.channels_per_pixel
        total = len(payload)
        position = 0
        while position < total:
            chunk = payload[position : position + protocol.DDP_MAX_DATA_LEN]
            self._sequence = self._sequence % 15 + 1
            packet = protocol.build_ddp(
                base + position,
                bytes(chunk),
                rgbw=rgbw,
                sequence=self._sequence,
                push=position + len(chunk) >= total,
            )
            position += len(chunk)
            yield self.address, packet

    def _build_artnet(self, payload) -> Iterator[tuple[tuple[str, int], bytes]]:
        self._sequence = self._sequence % 255 + 1
        size = self.config.channels_per_universe
        universe = self.config.universe
        for position in range(0, len(payload), size):
            packet = protocol.build_artnet_dmx(
                universe,
                bytes(payload[position : position + size]),
                sequence=self._sequence,
            )
            universe += 1
            yield self.address, packet

    def _build_e131(self, payload) -> Iterator[tuple[tuple[str, int], bytes]]:
        size = self.config.channels_per_universe
        universe = self.config.universe
        for position in range(0, len(payload), size):
            sequence = (self._universe_sequences.get(universe, 0) + 1) & 0xFF
            self._universe_sequences[universe] = sequence
            packet = protocol.build_e131(
                universe,
                bytes(payload[position : position + size]),
                sequence=sequence,
                cid=self._cid,
                source_name=self._source_name,
                priority=self.config.priority,
            )
            destination = (
                (protocol.e131_multicast_group(universe), self.config.port)
                if self.config.multicast
                else self.address
            )
            universe += 1
            yield destination, packet


def _stable_cid(name: str) -> bytes:
    """A deterministic RFC 4122 style CID so restarts keep the same identity."""
    digest = bytearray(hashlib.sha1(f"wled-proxy/{name}".encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    return bytes(digest)


class Router:
    """Renders the canvas to every enabled target."""

    def __init__(self, canvas: Canvas, targets: list[Target]):
        self.canvas = canvas
        self.targets = targets
        self.frames = RateCounter()

    @classmethod
    def build(cls, canvas: Canvas, configs: list[TargetConfig]) -> Router:
        return cls(canvas, [Target(cfg, canvas) for cfg in configs if cfg.enabled])

    def open(self) -> None:
        for target in self.targets:
            target.open()

    def close(self) -> None:
        for target in self.targets:
            target.close()

    async def resolve_all(self) -> None:
        await asyncio.gather(*(t.resolve() for t in self.targets))

    def send_frame(self) -> None:
        view = self.canvas.view
        for target in self.targets:
            target.send(view)
        self.frames.add()

    @property
    def packets_per_frame(self) -> int:
        return sum(t.packets_per_frame for t in self.targets)


def describe_coverage(
    led_count: int, targets: list[TargetConfig]
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Ranges of the virtual strip that no target covers, and those covered more than once."""
    counts = bytearray(led_count)
    for target in targets:
        start, end = target.start, min(target.end, led_count)
        counts[start:end] = counts[start:end].translate(_INCREMENT)
    return _runs(counts, 0), _runs(counts, 2, at_least=True)


_INCREMENT = bytes(min(255, i + 1) for i in range(256))


def _runs(
    counts: bytearray, value: int, at_least: bool = False
) -> list[tuple[int, int]]:
    runs = []
    start = None
    for i, count in enumerate(counts):
        hit = count >= value if at_least else count == value
        if hit and start is None:
            start = i
        elif not hit and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(counts) - 1))
    return runs
