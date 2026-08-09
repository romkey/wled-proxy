"""Wire formats for DDP, Art-Net and E1.31 (sACN).

Constants and framing here deliberately mirror WLED's own implementation
(``wled00/udp.cpp``, ``wled00/e131.cpp``, ``ESPAsyncE131.h``) so packets
produced by the proxy are byte-identical to what one WLED sends to another,
and so packets accepted by the proxy are exactly those WLED would accept.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

DDP_PORT = 4048
ARTNET_PORT = 6454
E131_PORT = 5568

# --- DDP ------------------------------------------------------------------

DDP_HEADER_LEN = 10
DDP_FLAGS_VER_MASK = 0xC0
DDP_FLAGS_VER1 = 0x40
DDP_FLAGS_PUSH = 0x01
DDP_FLAGS_QUERY = 0x02
DDP_FLAGS_REPLY = 0x04
DDP_FLAGS_STORAGE = 0x08
DDP_FLAGS_TIME = 0x10

DDP_TYPE_UNDEF = 0x00
DDP_TYPE_RGB24 = 0x0B
DDP_TYPE_RGBW32 = 0x1B

DDP_ID_DISPLAY = 1
DDP_ID_CONTROL = 246
DDP_ID_CONFIG = 250
DDP_ID_STATUS = 251

# 1440 bytes keeps a full packet under the 1500 byte Ethernet MTU.
DDP_MAX_DATA_LEN = 1440

_DDP_HEADER = struct.Struct(">BBBBIH")


@dataclass(frozen=True)
class DDPData:
    offset: int
    """Destination offset of this packet's payload, in channels (bytes)."""

    data: memoryview
    channels_per_pixel: int
    push: bool
    sequence: int


def parse_ddp(packet: memoryview | bytes) -> DDPData | None:
    """Parse a DDP data packet, or return None if it carries no pixel data."""
    if len(packet) < DDP_HEADER_LEN:
        return None
    flags, sequence, data_type, destination, offset, data_len = _DDP_HEADER.unpack_from(packet)

    if flags & DDP_FLAGS_VER_MASK != DDP_FLAGS_VER1:
        return None
    if flags & (DDP_FLAGS_QUERY | DDP_FLAGS_REPLY):
        return None
    if destination in (DDP_ID_CONTROL, DDP_ID_CONFIG, DDP_ID_STATUS):
        return None

    push = bool(flags & DDP_FLAGS_PUSH)
    if flags & DDP_FLAGS_STORAGE and not push:
        return None

    start = DDP_HEADER_LEN + (4 if flags & DDP_FLAGS_TIME else 0)
    if len(packet) < start + data_len:
        return None

    # Bits 5:3 of the data type select the colour format; anything other than
    # RGBW is treated as 24 bit RGB, which is what WLED does too.
    cpp = 4 if (data_type & 0b0011_1000) >> 3 == 0b011 else 3

    view = packet if isinstance(packet, memoryview) else memoryview(packet)
    return DDPData(
        offset=offset,
        data=view[start:start + data_len],
        channels_per_pixel=cpp,
        push=push,
        sequence=sequence & 0x0F,
    )


def build_ddp(offset: int, data: bytes, *, rgbw: bool, sequence: int, push: bool) -> bytes:
    """Build one DDP data packet addressed at ``offset`` channels into the display."""
    header = _DDP_HEADER.pack(
        DDP_FLAGS_VER1 | (DDP_FLAGS_PUSH if push else 0),
        sequence & 0x0F,
        DDP_TYPE_RGBW32 if rgbw else DDP_TYPE_RGB24,
        DDP_ID_DISPLAY,
        offset,
        len(data),
    )
    return header + data


# --- Art-Net --------------------------------------------------------------

ARTNET_ID = b"Art-Net\x00"
ARTNET_OPCODE_OPDMX = 0x5000
ARTNET_OPCODE_OPPOLL = 0x2000
ARTNET_OPCODE_OPPOLLREPLY = 0x2100
ARTNET_OPCODE_OPSYNC = 0x5200
ARTNET_PROTOCOL_VERSION = 14
ARTNET_HEADER_LEN = 18
ARTNET_MAX_CHANNELS = 512

# WLED sends 510 channels per universe for RGB (170 LEDs) and 512 for RGBW
# (128 LEDs); its receiver derives a universe's first LED from those same
# numbers, so senders must use them or pixels land in the wrong place.
ARTNET_CHANNELS_RGB = 510
ARTNET_CHANNELS_RGBW = 512


@dataclass(frozen=True)
class ArtnetData:
    universe: int
    sequence: int
    data: memoryview


def artnet_opcode(packet: memoryview | bytes) -> int | None:
    """Return the Art-Net opcode of a packet, or None if it is not Art-Net."""
    if len(packet) < 10 or bytes(packet[:8]) != ARTNET_ID:
        return None
    return int.from_bytes(packet[8:10], "little")


def parse_artnet_dmx(packet: memoryview | bytes) -> ArtnetData | None:
    """Parse an ArtDmx packet, or return None if it is malformed."""
    if len(packet) < ARTNET_HEADER_LEN:
        return None
    if artnet_opcode(packet) != ARTNET_OPCODE_OPDMX:
        return None

    length = int.from_bytes(packet[16:18], "big")
    length = min(length, len(packet) - ARTNET_HEADER_LEN, ARTNET_MAX_CHANNELS)
    if length <= 0:
        return None

    view = packet if isinstance(packet, memoryview) else memoryview(packet)
    return ArtnetData(
        universe=(packet[15] << 8) | packet[14],
        sequence=packet[12],
        data=view[ARTNET_HEADER_LEN:ARTNET_HEADER_LEN + length],
    )


def build_artnet_dmx(universe: int, data: bytes, *, sequence: int, physical: int = 0) -> bytes:
    """Build an ArtDmx packet. Odd length payloads are padded, as the spec requires."""
    if len(data) & 1:
        data = bytes(data) + b"\x00"
    return b"".join((
        ARTNET_ID,
        ARTNET_OPCODE_OPDMX.to_bytes(2, "little"),
        ARTNET_PROTOCOL_VERSION.to_bytes(2, "big"),
        bytes((sequence & 0xFF, physical, universe & 0xFF, (universe >> 8) & 0x7F)),
        len(data).to_bytes(2, "big"),
        bytes(data),
    ))


# --- E1.31 (sACN) ---------------------------------------------------------

E131_ACN_ID = b"ASC-E1.17\x00\x00\x00"
E131_VECTOR_ROOT_DATA = 0x00000004
E131_VECTOR_FRAME_DATA = 0x00000002
E131_VECTOR_DMP_SET_PROPERTY = 0x02
E131_HEADER_LEN = 126  # through the DMX start code
E131_OPTION_PREVIEW = 0x80
E131_OPTION_TERMINATED = 0x40
E131_MAX_CHANNELS = 512
E131_DEFAULT_PRIORITY = 100

_E131_ROOT = struct.Struct(">HH12sHI16s")
_E131_FRAME = struct.Struct(">HI64sBHBBH")
_E131_DMP = struct.Struct(">HBBHHH")


@dataclass(frozen=True)
class E131Data:
    universe: int
    sequence: int
    priority: int
    options: int
    data: memoryview
    """DMX channel data, with the start code already stripped."""


def parse_e131(packet: memoryview | bytes) -> E131Data | None:
    """Parse an E1.31 data packet, or return None if it carries no DMX levels."""
    if len(packet) < E131_HEADER_LEN:
        return None
    if bytes(packet[4:16]) != E131_ACN_ID:
        return None
    if int.from_bytes(packet[18:22], "big") != E131_VECTOR_ROOT_DATA:
        return None
    if int.from_bytes(packet[40:44], "big") != E131_VECTOR_FRAME_DATA:
        return None
    if packet[117] != E131_VECTOR_DMP_SET_PROPERTY:
        return None
    if packet[125] != 0:  # only zero start code carries DMX levels
        return None

    options = packet[112]
    if options & E131_OPTION_PREVIEW:
        return None

    count = int.from_bytes(packet[123:125], "big") - 1
    count = min(count, len(packet) - E131_HEADER_LEN, E131_MAX_CHANNELS)
    if count <= 0:
        return None

    view = packet if isinstance(packet, memoryview) else memoryview(packet)
    return E131Data(
        universe=int.from_bytes(packet[113:115], "big"),
        sequence=packet[111],
        priority=packet[108],
        options=options,
        data=view[E131_HEADER_LEN:E131_HEADER_LEN + count],
    )


def build_e131(
    universe: int,
    data: bytes,
    *,
    sequence: int,
    cid: bytes,
    source_name: bytes,
    priority: int = E131_DEFAULT_PRIORITY,
    sync_address: int = 0,
    options: int = 0,
) -> bytes:
    """Build an E1.31 data packet carrying ``data`` as zero start code DMX levels."""
    total = E131_HEADER_LEN + len(data)
    root = _E131_ROOT.pack(
        0x0010,                     # preamble size
        0x0000,                     # postamble size
        E131_ACN_ID,
        0x7000 | (total - 16),
        E131_VECTOR_ROOT_DATA,
        cid,
    )
    frame = _E131_FRAME.pack(
        0x7000 | (total - 38),
        E131_VECTOR_FRAME_DATA,
        source_name,
        priority,
        sync_address,
        sequence & 0xFF,
        options,
        universe,
    )
    dmp = _E131_DMP.pack(
        0x7000 | (total - 115),
        E131_VECTOR_DMP_SET_PROPERTY,
        0xA1,                       # address type & data type
        0x0000,                     # first property address
        0x0001,                     # address increment
        len(data) + 1,              # property value count, including start code
    )
    return root + frame + dmp + b"\x00" + data


def e131_multicast_group(universe: int) -> str:
    """Multicast address reserved for an sACN universe (E1.31 section 9.3.1)."""
    return f"239.255.{(universe >> 8) & 0xFF}.{universe & 0xFF}"
