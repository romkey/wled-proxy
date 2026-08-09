import pytest

from wled_proxy import protocol
from wled_proxy.config import ArtnetInputConfig, DDPInputConfig, E131InputConfig
from wled_proxy.inputs import ArtnetInput, DDPInput, E131Input, UniverseMap
from wled_proxy.pixels import Canvas


class Recorder:
    def __init__(self):
        self.writes = 0
        self.commits = 0

    def write(self):
        self.writes += 1

    def commit(self):
        self.commits += 1


def make(cls, config, led_count=1000, fmt="rgb"):
    canvas = Canvas(led_count, fmt)
    events = Recorder()
    return cls(config, canvas, events.write, events.commit), canvas, events


def test_universe_map_matches_wled_multi_rgb_layout():
    universes = UniverseMap(start_universe=1, channels_per_universe=510,
                            dmx_start_channel=1, channels_per_pixel=3)
    assert universes.locate(0) is None
    assert universes.locate(1) == (0, 0, 510)
    assert universes.locate(2) == (170 * 3, 0, 510)
    assert universes.locate(5) == (170 * 4 * 3, 0, 510)
    assert universes.universe_count(170) == 1
    assert universes.universe_count(171) == 2
    assert universes.universe_count(1000) == 6


def test_universe_map_for_rgbw():
    universes = UniverseMap(0, 512, 1, 4)
    assert universes.locate(0) == (0, 0, 512)
    assert universes.locate(1) == (128 * 4, 0, 512)
    assert universes.universe_count(128) == 1
    assert universes.universe_count(129) == 2


def test_universe_map_with_a_dmx_start_address():
    universes = UniverseMap(1, 510, 10, 3)
    offset, skip, limit = universes.locate(1)
    assert (offset, skip) == (0, 9)
    assert universes.locate(2)[0] == 167 * 3  # (512 - 10 + 1) // 3 pixels in universe 1


def test_ddp_input_writes_and_commits_on_push():
    handler, canvas, events = make(DDPInput, DDPInputConfig(format="rgb"))
    handler.datagram_received(
        protocol.build_ddp(0, b"\x01\x02\x03", rgbw=False, sequence=1, push=False),
        ("10.0.0.9", 4048))
    assert canvas.buffer[0:3] == b"\x01\x02\x03"
    assert events.commits == 0

    handler.datagram_received(
        protocol.build_ddp(3, b"\x04\x05\x06", rgbw=False, sequence=2, push=True),
        ("10.0.0.9", 4048))
    assert canvas.buffer[0:6] == b"\x01\x02\x03\x04\x05\x06"
    assert events.commits == 1
    assert handler.last_source == "10.0.0.9"
    assert handler.rejected == 0


def test_ddp_input_counts_rejected_packets():
    handler, _, _ = make(DDPInput, DDPInputConfig())
    handler.datagram_received(b"junk", ("10.0.0.9", 4048))
    assert handler.rejected == 1
    assert handler.packets.total == 1


def test_ddp_input_applies_a_channel_offset():
    handler, canvas, _ = make(DDPInput, DDPInputConfig(format="rgb", channel_offset=30))
    handler.datagram_received(
        protocol.build_ddp(0, b"\x09\x09\x09", rgbw=False, sequence=1, push=True),
        ("10.0.0.9", 4048))
    assert canvas.buffer[30:33] == b"\x09\x09\x09"


def test_artnet_input_places_universes_end_to_end():
    config = ArtnetInputConfig(format="rgb", start_universe=0)
    handler, canvas, events = make(ArtnetInput, config, led_count=400)
    handler.datagram_received(
        protocol.build_artnet_dmx(0, b"\xaa" * 510, sequence=1), ("10.0.0.9", 6454))
    handler.datagram_received(
        protocol.build_artnet_dmx(1, b"\xbb" * 510, sequence=1), ("10.0.0.9", 6454))

    assert canvas.buffer[0:510] == b"\xaa" * 510
    assert canvas.buffer[510:1020] == b"\xbb" * 510
    assert events.commits == 0  # 400 pixels need three universes

    handler.datagram_received(
        protocol.build_artnet_dmx(2, b"\xcc" * 510, sequence=1), ("10.0.0.9", 6454))
    assert events.commits == 1


def test_artnet_input_ignores_other_opcodes_and_syncs():
    handler, _, events = make(ArtnetInput, ArtnetInputConfig())
    poll = protocol.ARTNET_ID + b"\x00\x20" + bytes(6)
    handler.datagram_received(poll, ("10.0.0.9", 6454))
    assert handler.rejected == 0
    assert events.commits == 0

    sync = protocol.ARTNET_ID + b"\x00\x52" + bytes(6)
    handler.datagram_received(sync, ("10.0.0.9", 6454))
    assert events.commits == 1


def test_artnet_input_ignores_universes_below_the_start():
    handler, canvas, _ = make(ArtnetInput, ArtnetInputConfig(start_universe=4))
    handler.datagram_received(
        protocol.build_artnet_dmx(3, b"\xff" * 510, sequence=1), ("10.0.0.9", 6454))
    assert handler.rejected == 1
    assert canvas.buffer == bytes(len(canvas.buffer))


def test_artnet_input_truncates_oversized_universes():
    """A sender using 512 channels per universe must not bleed into the next one."""
    handler, canvas, _ = make(ArtnetInput, ArtnetInputConfig(start_universe=0))
    handler.datagram_received(
        protocol.build_artnet_dmx(0, b"\xff" * 512, sequence=1), ("10.0.0.9", 6454))
    assert canvas.buffer[509] == 0xFF
    assert canvas.buffer[510:512] == b"\x00\x00"


def test_e131_input_places_universes_and_honours_priority():
    config = E131InputConfig(format="rgb", start_universe=1, min_priority=50)
    handler, canvas, events = make(E131Input, config, led_count=340)

    def packet(universe, fill, priority=100):
        return protocol.build_e131(universe, fill, sequence=1, cid=bytes(16),
                                   source_name=bytes(64), priority=priority)

    handler.datagram_received(packet(1, b"\x11" * 510), ("10.0.0.9", 5568))
    handler.datagram_received(packet(2, b"\x22" * 510), ("10.0.0.9", 5568))
    assert canvas.buffer[0:510] == b"\x11" * 510
    assert canvas.buffer[510:1020] == b"\x22" * 510
    assert events.commits == 1

    handler.datagram_received(packet(1, b"\x33" * 510, priority=10), ("10.0.0.9", 5568))
    assert canvas.buffer[0:510] == b"\x11" * 510


def test_e131_input_converts_an_rgbw_stream_onto_an_rgb_strip():
    config = E131InputConfig(format="rgbw", start_universe=1, channels_per_universe=512)
    handler, canvas, _ = make(E131Input, config, led_count=10, fmt="rgb")
    handler.datagram_received(
        protocol.build_e131(1, bytes([1, 2, 3, 250, 4, 5, 6, 250]), sequence=1,
                            cid=bytes(16), source_name=bytes(64)),
        ("10.0.0.9", 5568))
    assert canvas.buffer[0:6] == bytes([1, 2, 3, 4, 5, 6])
