"""Send a test pattern at the proxy, so you can light strips up without a
master WLED or a show controller in the way.

    python -m wled_proxy.testpattern --host 127.0.0.1 --count 1200
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

from . import protocol
from .config import DEFAULT_PORTS

PATTERNS = ("rainbow", "chase", "sweep", "solid", "index")


def rainbow(count: int, phase: float) -> bytes:
    out = bytearray(count * 3)
    for i in range(count):
        hue = (i / max(count, 1) + phase) % 1.0
        out[i * 3 : i * 3 + 3] = _hsv(hue)
    return bytes(out)


def chase(count: int, phase: float, width: int = 12) -> bytes:
    out = bytearray(count * 3)
    head = int(phase * count) % max(count, 1)
    for offset in range(width):
        level = 255 - int(255 * offset / width)
        i = (head - offset) % count
        out[i * 3 : i * 3 + 3] = bytes((level, level, level))
    return bytes(out)


def sweep(count: int, phase: float) -> bytes:
    """A single lit pixel plus a marker every 100, for finding wiring order."""
    out = bytearray(count * 3)
    for i in range(0, count, 100):
        out[i * 3 : i * 3 + 3] = b"\x20\x00\x20"
    i = int(phase * count) % max(count, 1)
    out[i * 3 : i * 3 + 3] = b"\xff\xff\xff"
    return bytes(out)


def solid(count: int, color: tuple[int, int, int]) -> bytes:
    return bytes(color) * count


def index(count: int, _phase: float) -> bytes:
    """Every 50th pixel red, every 10th green: a ruler along the strip."""
    out = bytearray(count * 3)
    for i in range(count):
        if i % 50 == 0:
            out[i * 3 : i * 3 + 3] = b"\xff\x00\x00"
        elif i % 10 == 0:
            out[i * 3 : i * 3 + 3] = b"\x00\x40\x00"
    return bytes(out)


def _hsv(hue: float) -> bytes:
    r, g, b = (
        max(0.0, min(1.0, 1.5 - abs(((hue + shift) % 1.0) * 6.0 - 3.0)))
        for shift in (0.0, 2 / 3, 1 / 3)
    )
    return bytes((int(r * 255), int(g * 255), int(b * 255)))


def to_rgbw(rgb: bytes) -> bytes:
    out = bytearray(len(rgb) // 3 * 4)
    out[0::4] = rgb[0::3]
    out[1::4] = rgb[1::3]
    out[2::4] = rgb[2::3]
    return bytes(out)


def send_ddp(sock, address, payload: bytes, rgbw: bool, sequence: int) -> int:
    total = len(payload)
    position = 0
    while position < total:
        chunk = payload[position : position + protocol.DDP_MAX_DATA_LEN]
        sequence = sequence % 15 + 1
        position += len(chunk)
        sock.sendto(
            protocol.build_ddp(
                position - len(chunk),
                chunk,
                rgbw=rgbw,
                sequence=sequence,
                push=position >= total,
            ),
            address,
        )
    return sequence


def send_artnet(
    sock, address, payload: bytes, size: int, universe: int, sequence: int
) -> int:
    sequence = sequence % 255 + 1
    for position in range(0, len(payload), size):
        sock.sendto(
            protocol.build_artnet_dmx(
                universe, payload[position : position + size], sequence=sequence
            ),
            address,
        )
        universe += 1
    return sequence


def send_e131(
    sock,
    address,
    payload: bytes,
    size: int,
    universe: int,
    sequences: dict,
    cid: bytes,
    source: bytes,
) -> None:
    for position in range(0, len(payload), size):
        sequences[universe] = (sequences.get(universe, 0) + 1) & 0xFF
        sock.sendto(
            protocol.build_e131(
                universe,
                payload[position : position + size],
                sequence=sequences[universe],
                cid=cid,
                source_name=source,
            ),
            address,
        )
        universe += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wled-proxy-testpattern",
        description="Send a moving test pattern to a wled-proxy (or straight to a WLED).",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--protocol", choices=("ddp", "artnet", "e131"), default="ddp")
    parser.add_argument(
        "--count", type=int, default=300, help="number of pixels to send"
    )
    parser.add_argument("--format", choices=("rgb", "rgbw"), default="rgb")
    parser.add_argument("--pattern", choices=PATTERNS, default="rainbow")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument(
        "--speed", type=float, default=0.2, help="pattern cycles per second"
    )
    parser.add_argument("--brightness", type=float, default=1.0)
    parser.add_argument(
        "--color", default="255,255,255", help="colour for --pattern solid"
    )
    parser.add_argument("--universe", type=int, help="first universe for artnet/e131")
    parser.add_argument(
        "--duration", type=float, default=0.0, help="seconds to run, 0 = forever"
    )
    args = parser.parse_args(argv)

    port = args.port or DEFAULT_PORTS[args.protocol]
    address = (args.host, port)
    universe = (
        args.universe
        if args.universe is not None
        else (0 if args.protocol == "artnet" else 1)
    )
    universe_size = (
        protocol.ARTNET_CHANNELS_RGBW
        if args.format == "rgbw"
        else protocol.ARTNET_CHANNELS_RGB
    )
    try:
        color = tuple(int(v) & 0xFF for v in args.color.split(","))
        if len(color) != 3:
            raise ValueError
    except ValueError:
        print(
            "--color needs three comma separated numbers, e.g. 255,80,0",
            file=sys.stderr,
        )
        return 2

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    scale = max(0.0, min(1.0, args.brightness))
    lut = bytes(int(i * scale) for i in range(256))
    interval = 1.0 / args.fps
    sequence = 0
    sequences: dict[int, int] = {}
    cid = bytes(range(16))
    source = b"wled-proxy testpattern".ljust(64, b"\x00")

    print(
        f"sending {args.pattern} to {args.host}:{port} over {args.protocol.upper()} "
        f"({args.count} {args.format.upper()} pixels at {args.fps:g} fps), ctrl-c to stop"
    )
    started = time.monotonic()
    frames = 0
    try:
        while True:
            now = time.monotonic()
            if args.duration and now - started >= args.duration:
                break
            phase = (now - started) * args.speed
            if args.pattern == "solid":
                frame = solid(args.count, color)
            elif args.pattern == "chase":
                frame = chase(args.count, phase)
            elif args.pattern == "sweep":
                frame = sweep(args.count, phase)
            elif args.pattern == "index":
                frame = index(args.count, phase)
            else:
                frame = rainbow(args.count, phase)
            frame = frame.translate(lut)
            if args.format == "rgbw":
                frame = to_rgbw(frame)

            if args.protocol == "ddp":
                sequence = send_ddp(
                    sock, address, frame, args.format == "rgbw", sequence
                )
            elif args.protocol == "artnet":
                sequence = send_artnet(
                    sock, address, frame, universe_size, universe, sequence
                )
            else:
                send_e131(
                    sock,
                    address,
                    frame,
                    universe_size,
                    universe,
                    sequences,
                    cid,
                    source,
                )

            frames += 1
            sleep = interval - (time.monotonic() - now)
            if sleep > 0:
                time.sleep(sleep)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    elapsed = time.monotonic() - started
    print(
        f"\nsent {frames} frames in {elapsed:.1f}s ({frames / max(elapsed, 1e-6):.1f} fps)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
