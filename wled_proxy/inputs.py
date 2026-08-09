"""UDP receivers that paint incoming pixel data onto the virtual strip."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct
from collections.abc import Callable

from . import protocol
from .config import ArtnetInputConfig, DDPInputConfig, E131InputConfig
from .metrics import RateCounter
from .pixels import Canvas

log = logging.getLogger(__name__)

RECEIVE_BUFFER_BYTES = 1 << 20


class UniverseMap:
    """Maps DMX universes onto positions in the virtual strip.

    The arithmetic matches WLED's "Multiple RGB" DMX mode: the first universe
    starts at the configured DMX address and every later universe starts at
    channel 1, with each universe holding a whole number of pixels.
    """

    def __init__(
        self,
        start_universe: int,
        channels_per_universe: int,
        dmx_start_channel: int,
        channels_per_pixel: int,
    ):
        self.start_universe = start_universe
        self.channels_per_pixel = channels_per_pixel
        self.dmx_start_channel = dmx_start_channel
        self.leds_per_universe = channels_per_universe // channels_per_pixel
        self.first_universe_leds = (
            min(channels_per_universe, 513 - dmx_start_channel) // channels_per_pixel
        )

    def locate(self, universe: int) -> tuple[int, int, int] | None:
        """For a universe, the channel offset into the strip, the offset of the
        first usable byte in the DMX data, and how many channels to take.
        Returns None for universes outside the configured range.
        """
        index = universe - self.start_universe
        if index < 0:
            return None
        if index == 0:
            return (
                0,
                self.dmx_start_channel - 1,
                self.first_universe_leds * self.channels_per_pixel,
            )
        pixel = self.first_universe_leds + (index - 1) * self.leds_per_universe
        offset = pixel * self.channels_per_pixel
        return offset, 0, self.leds_per_universe * self.channels_per_pixel

    def universe_count(self, led_count: int) -> int:
        """How many universes are needed to cover ``led_count`` pixels."""
        if led_count <= self.first_universe_leds or self.leds_per_universe <= 0:
            return 1
        remaining = led_count - self.first_universe_leds
        return 1 + -(-remaining // self.leds_per_universe)


class InputBase(asyncio.DatagramProtocol):
    """Shared plumbing: counters, error handling and frame notification."""

    protocol_name = "input"

    def __init__(
        self,
        canvas: Canvas,
        on_write: Callable[[], None],
        on_commit: Callable[[], None],
    ):
        self.canvas = canvas
        self._on_write = on_write
        self._on_commit = on_commit
        self.transport: asyncio.DatagramTransport | None = None
        self.packets = RateCounter()
        self.frames = RateCounter()
        self.channels_written = 0
        self.rejected = 0
        self.last_source: str | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def error_received(self, exc: Exception) -> None:
        log.debug("%s input socket error: %s", self.protocol_name, exc)

    def datagram_received(self, data: bytes, addr) -> None:
        self.packets.add()
        self.last_source = addr[0]
        try:
            if not self.handle(memoryview(data)):
                self.rejected += 1
        except Exception:
            self.rejected += 1
            log.debug(
                "%s input: bad packet from %s",
                self.protocol_name,
                addr[0],
                exc_info=True,
            )

    def handle(self, data: memoryview) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def _store(self, channel_offset: int, data: memoryview, channels_per_pixel: int) -> None:
        self.channels_written += self.canvas.write(channel_offset, data, channels_per_pixel)
        self._on_write()

    def _commit(self) -> None:
        self.frames.add()
        self._on_commit()


class DDPInput(InputBase):
    protocol_name = "ddp"

    def __init__(self, config: DDPInputConfig, canvas: Canvas, on_write, on_commit):
        super().__init__(canvas, on_write, on_commit)
        self.config = config
        self.channels_per_pixel = len(config.format)

    def handle(self, data: memoryview) -> bool:
        packet = protocol.parse_ddp(data)
        if packet is None:
            return False
        self._store(
            self.config.channel_offset + packet.offset,
            packet.data,
            packet.channels_per_pixel,
        )
        if packet.push:
            self._commit()
        return True


class ArtnetInput(InputBase):
    protocol_name = "artnet"

    def __init__(self, config: ArtnetInputConfig, canvas: Canvas, on_write, on_commit):
        super().__init__(canvas, on_write, on_commit)
        self.config = config
        self.channels_per_pixel = len(config.format)
        self.universes = UniverseMap(
            config.start_universe,
            config.channels_per_universe,
            config.dmx_start_channel,
            self.channels_per_pixel,
        )
        self.last_universe = (
            config.start_universe + self.universes.universe_count(canvas.led_count) - 1
        )

    def handle(self, data: memoryview) -> bool:
        opcode = protocol.artnet_opcode(data)
        if opcode is None:
            return False
        if opcode == protocol.ARTNET_OPCODE_OPSYNC:
            self._commit()
            return True
        if opcode != protocol.ARTNET_OPCODE_OPDMX:
            return True  # polls and other opcodes are simply not our business

        packet = protocol.parse_artnet_dmx(data)
        if packet is None:
            return False
        placement = self.universes.locate(packet.universe)
        if placement is None:
            return False
        offset, skip, limit = placement
        self._store(offset, packet.data[skip : skip + limit], self.channels_per_pixel)
        if packet.universe >= self.last_universe:
            self._commit()
        return True


class E131Input(InputBase):
    protocol_name = "e131"

    def __init__(self, config: E131InputConfig, canvas: Canvas, on_write, on_commit):
        super().__init__(canvas, on_write, on_commit)
        self.config = config
        self.channels_per_pixel = len(config.format)
        self.universes = UniverseMap(
            config.start_universe,
            config.channels_per_universe,
            config.dmx_start_channel,
            self.channels_per_pixel,
        )
        self.universe_count = self.universes.universe_count(canvas.led_count)
        self.last_universe = config.start_universe + self.universe_count - 1

    def handle(self, data: memoryview) -> bool:
        packet = protocol.parse_e131(data)
        if packet is None:
            return False
        if packet.priority < self.config.min_priority:
            return True
        placement = self.universes.locate(packet.universe)
        if placement is None:
            return False
        offset, skip, limit = placement
        self._store(offset, packet.data[skip : skip + limit], self.channels_per_pixel)
        if packet.universe >= self.last_universe:
            self._commit()
        return True


def _make_socket(bind: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # SO_REUSEADDR lets sACN multicast share the port with other receivers on
    # the box. SO_REUSEPORT is deliberately not set: on Linux it would let a
    # second instance silently bind the same port and steal half the packets.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER_BYTES)
    try:
        sock.bind((bind, port))
    except OSError as exc:
        sock.close()
        raise OSError(f"cannot bind UDP {bind}:{port}: {exc}") from exc
    sock.setblocking(False)
    return sock


def join_multicast(sock: socket.socket, groups: list[str], interface: str) -> list[str]:
    """Join sACN multicast groups, returning the ones that succeeded."""
    joined, failed = [], []
    for group in groups:
        request = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(interface))
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, request)
        except OSError as exc:
            failed.append((group, exc))
            continue
        joined.append(group)
    if failed:
        group, exc = failed[0]
        log.warning(
            "could not join %d of %d sACN multicast groups (first was %s: %s). "
            "Linux allows 20 memberships per socket by default; raise "
            "net.ipv4.igmp_max_memberships or use unicast sACN instead.",
            len(failed),
            len(groups),
            group,
            exc,
        )
    return joined


async def start_input(
    handler: InputBase,
    bind: str,
    port: int,
    multicast_groups: list[str] | None = None,
    multicast_interface: str = "0.0.0.0",
) -> InputBase:
    """Bind a socket and attach ``handler`` to it."""
    sock = _make_socket(bind, port)
    if multicast_groups:
        joined = join_multicast(sock, multicast_groups, multicast_interface)
        if joined:
            log.info(
                "joined %d sACN multicast group(s), %s - %s",
                len(joined),
                joined[0],
                joined[-1],
            )
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: handler, sock=sock)
    return handler
