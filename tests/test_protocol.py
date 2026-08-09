import pytest

from wled_proxy import protocol


def test_ddp_round_trip():
    payload = bytes(range(60))
    packet = protocol.build_ddp(300, payload, rgbw=False, sequence=7, push=True)

    assert packet[0] == protocol.DDP_FLAGS_VER1 | protocol.DDP_FLAGS_PUSH
    assert packet[1] == 7
    assert packet[2] == protocol.DDP_TYPE_RGB24
    assert packet[3] == protocol.DDP_ID_DISPLAY
    assert len(packet) == protocol.DDP_HEADER_LEN + len(payload)

    parsed = protocol.parse_ddp(packet)
    assert parsed.offset == 300
    assert parsed.channels_per_pixel == 3
    assert parsed.push is True
    assert parsed.sequence == 7
    assert bytes(parsed.data) == payload


def test_ddp_rgbw_type_is_detected():
    packet = protocol.build_ddp(0, bytes(8), rgbw=True, sequence=1, push=False)
    assert packet[2] == protocol.DDP_TYPE_RGBW32
    parsed = protocol.parse_ddp(packet)
    assert parsed.channels_per_pixel == 4
    assert parsed.push is False


@pytest.mark.parametrize(
    "mangle",
    [
        lambda p: p[:9],  # truncated header
        lambda p: bytes([0x00]) + p[1:],  # wrong version
        lambda p: bytes([p[0] | protocol.DDP_FLAGS_QUERY]) + p[1:],
        lambda p: p[:3] + bytes([protocol.DDP_ID_CONFIG]) + p[4:],
        lambda p: p[:12],  # payload shorter than declared
    ],
)
def test_ddp_rejects_bad_packets(mangle):
    packet = protocol.build_ddp(0, bytes(30), rgbw=False, sequence=1, push=True)
    assert protocol.parse_ddp(mangle(packet)) is None


def test_ddp_timecode_flag_shifts_the_payload():
    payload = bytes(range(12))
    packet = bytearray(protocol.build_ddp(0, payload, rgbw=False, sequence=1, push=True))
    packet[0] |= protocol.DDP_FLAGS_TIME
    packet[protocol.DDP_HEADER_LEN : protocol.DDP_HEADER_LEN] = b"\x00\x00\x00\x00"
    parsed = protocol.parse_ddp(bytes(packet))
    assert bytes(parsed.data) == payload


def test_artnet_round_trip():
    payload = bytes(range(200))
    packet = protocol.build_artnet_dmx(0x1234, payload, sequence=42)

    assert packet[:8] == protocol.ARTNET_ID
    assert packet[8:10] == b"\x00\x50"  # OpDmx, little endian
    assert packet[10:12] == b"\x00\x0e"  # protocol version 14, high byte first
    assert packet[12] == 42
    assert packet[14] == 0x34  # SubUni
    assert packet[15] == 0x12  # Net
    assert packet[16:18] == len(payload).to_bytes(2, "big")

    parsed = protocol.parse_artnet_dmx(packet)
    assert parsed.universe == 0x1234
    assert parsed.sequence == 42
    assert bytes(parsed.data) == payload


def test_artnet_pads_odd_length_payloads():
    packet = protocol.build_artnet_dmx(0, bytes(9), sequence=1)
    assert protocol.parse_artnet_dmx(packet).data.nbytes == 10


def test_artnet_opcode_and_rejection():
    poll = protocol.ARTNET_ID + b"\x00\x20" + bytes(6)
    assert protocol.artnet_opcode(poll) == protocol.ARTNET_OPCODE_OPPOLL
    assert protocol.parse_artnet_dmx(poll) is None
    assert protocol.artnet_opcode(b"not art-net at all") is None


def test_e131_round_trip():
    payload = bytes(range(256))
    packet = protocol.build_e131(
        7,
        payload,
        sequence=5,
        cid=bytes(range(16)),
        source_name=b"proxy".ljust(64, b"\x00"),
        priority=120,
    )

    assert len(packet) == protocol.E131_HEADER_LEN + len(payload)
    assert packet[0:2] == b"\x00\x10"  # preamble size
    assert packet[4:16] == protocol.E131_ACN_ID
    assert packet[108] == 120  # priority
    assert packet[111] == 5  # sequence
    assert packet[113:115] == (7).to_bytes(2, "big")  # universe
    assert packet[125] == 0  # DMX start code

    parsed = protocol.parse_e131(packet)
    assert parsed.universe == 7
    assert parsed.sequence == 5
    assert parsed.priority == 120
    assert bytes(parsed.data) == payload


def test_e131_pdu_lengths_match_the_standard():
    """A full 512 channel universe has well known PDU flags and length words."""
    packet = protocol.build_e131(1, bytes(512), sequence=1, cid=bytes(16), source_name=bytes(64))
    assert len(packet) == 638
    assert packet[16:18] == b"\x72\x6e"  # root layer
    assert packet[38:40] == b"\x72\x58"  # framing layer
    assert packet[115:117] == b"\x72\x0b"  # DMP layer
    assert packet[123:125] == (513).to_bytes(2, "big")


def test_e131_ignores_preview_and_non_zero_start_code():
    packet = bytearray(
        protocol.build_e131(1, bytes(30), sequence=1, cid=bytes(16), source_name=bytes(64))
    )
    preview = bytearray(packet)
    preview[112] = protocol.E131_OPTION_PREVIEW
    assert protocol.parse_e131(bytes(preview)) is None

    other_start_code = bytearray(packet)
    other_start_code[125] = 0xCC
    assert protocol.parse_e131(bytes(other_start_code)) is None

    assert protocol.parse_e131(bytes(packet[:100])) is None


def test_e131_multicast_groups():
    assert protocol.e131_multicast_group(1) == "239.255.0.1"
    assert protocol.e131_multicast_group(256) == "239.255.1.0"
    assert protocol.e131_multicast_group(0x1234) == "239.255.18.52"
