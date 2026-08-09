"""End to end: feed the proxy a frame and check what the targets receive."""

import asyncio
import functools
import json
import socket

from wled_proxy import protocol
from wled_proxy.app import Proxy
from wled_proxy.config import parse
from wled_proxy.status import StatusServer


def async_test(func):
    """Run an async test without pulling in an asyncio pytest plugin."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return wrapper


class FakeDevice:
    """A UDP socket standing in for a WLED device."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]

    def close(self):
        self.sock.close()

    async def collect(self, settle=0.15):
        """Everything received once the proxy has had time to send a frame."""
        await asyncio.sleep(settle)
        packets = []
        while True:
            try:
                packets.append(self.sock.recv(2048))
            except BlockingIOError:
                return packets


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ramp(count):
    return bytes((i * 3) % 256 for i in range(count * 3))


def send_ddp_frame(port, frame):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for offset in range(0, len(frame), protocol.DDP_MAX_DATA_LEN):
        chunk = frame[offset:offset + protocol.DDP_MAX_DATA_LEN]
        sender.sendto(
            protocol.build_ddp(offset, chunk, rgbw=False, sequence=1,
                               push=offset + len(chunk) >= len(frame)),
            ("127.0.0.1", port))
    sender.close()


@async_test
async def test_ddp_in_splits_across_three_ddp_targets():
    devices = [FakeDevice() for _ in range(3)]
    input_port = free_port()
    proxy = Proxy(parse({
        "virtual_strip": {"led_count": 300},
        "inputs": {"ddp": {"port": input_port}},
        "status": {"enabled": False},
        "output": {"max_fps": 1000},
        "targets": [
            {"name": f"d{i}", "host": "127.0.0.1", "port": device.port, "count": 100}
            for i, device in enumerate(devices)
        ],
    }))
    await proxy.start()
    frame = ramp(300)
    try:
        send_ddp_frame(input_port, frame)
        for index, device in enumerate(devices):
            packets = await device.collect()
            assert len(packets) == 1, f"device {index} received {len(packets)} packets"
            parsed = protocol.parse_ddp(packets[0])
            assert parsed.push is True
            assert parsed.offset == 0
            assert bytes(parsed.data) == frame[index * 300:(index + 1) * 300]
    finally:
        await proxy.stop()
        for device in devices:
            device.close()


@async_test
async def test_ddp_in_artnet_and_e131_out():
    art, sacn = FakeDevice(), FakeDevice()
    input_port = free_port()
    proxy = Proxy(parse({
        "virtual_strip": {"led_count": 400},
        "inputs": {"ddp": {"port": input_port}},
        "status": {"enabled": False},
        "output": {"max_fps": 1000},
        "targets": [
            {"name": "art", "host": "127.0.0.1", "port": art.port,
             "protocol": "artnet", "count": 200, "start": 0, "universe": 0},
            {"name": "sacn", "host": "127.0.0.1", "port": sacn.port,
             "protocol": "e131", "count": 200, "start": 200, "universe": 1,
             "reverse": True},
        ],
    }))
    await proxy.start()
    frame = ramp(400)
    try:
        send_ddp_frame(input_port, frame)

        packets = sorted(await art.collect(), key=lambda p: p[14])
        assert len(packets) == 2
        first, second = (protocol.parse_artnet_dmx(p) for p in packets)
        assert (first.universe, second.universe) == (0, 1)
        assert bytes(first.data) == frame[0:510]
        assert bytes(second.data) == frame[510:600]

        packets = sorted(await sacn.collect(), key=lambda p: p[114])
        assert len(packets) == 2
        first, second = (protocol.parse_e131(p) for p in packets)
        assert (first.universe, second.universe) == (1, 2)
        assert first.priority == 100
        expected = b"".join(frame[i * 3:i * 3 + 3] for i in reversed(range(200, 400)))
        assert bytes(first.data) + bytes(second.data) == expected
    finally:
        await proxy.stop()
        art.close()
        sacn.close()


@async_test
async def test_artnet_in_fans_out_to_an_rgbw_target():
    device = FakeDevice()
    input_port = free_port()
    proxy = Proxy(parse({
        "virtual_strip": {"led_count": 170},
        "inputs": {"ddp": {"enabled": False},
                   "artnet": {"enabled": True, "port": input_port, "start_universe": 0}},
        "status": {"enabled": False},
        "output": {"max_fps": 1000},
        "targets": [{"name": "w", "host": "127.0.0.1", "port": device.port,
                     "count": 170, "format": "rgbw", "white_mode": "brighter"}],
    }))
    await proxy.start()
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(protocol.build_artnet_dmx(0, bytes((90, 40, 60)) * 170, sequence=1),
                      ("127.0.0.1", input_port))
        sender.close()

        packets = await device.collect()
        assert len(packets) == 1
        parsed = protocol.parse_ddp(packets[0])
        assert parsed.channels_per_pixel == 4
        assert bytes(parsed.data) == bytes((90, 40, 60, 40)) * 170
    finally:
        await proxy.stop()
        device.close()


@async_test
async def test_frame_timeout_sends_even_without_a_push():
    device = FakeDevice()
    input_port = free_port()
    proxy = Proxy(parse({
        "virtual_strip": {"led_count": 10},
        "inputs": {"ddp": {"port": input_port}},
        "status": {"enabled": False},
        "output": {"max_fps": 1000, "frame_timeout_ms": 20},
        "targets": [{"name": "d", "host": "127.0.0.1", "port": device.port, "count": 10}],
    }))
    await proxy.start()
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(protocol.build_ddp(0, b"\x07" * 30, rgbw=False, sequence=1, push=False),
                      ("127.0.0.1", input_port))
        sender.close()
        packets = await device.collect()
        assert len(packets) == 1
        assert bytes(protocol.parse_ddp(packets[0]).data) == b"\x07" * 30
    finally:
        await proxy.stop()
        device.close()


@async_test
async def test_idle_strip_sends_nothing():
    device = FakeDevice()
    proxy = Proxy(parse({
        "virtual_strip": {"led_count": 10},
        "inputs": {"ddp": {"port": free_port()}},
        "status": {"enabled": False},
        "targets": [{"name": "d", "host": "127.0.0.1", "port": device.port, "count": 10}],
    }))
    await proxy.start()
    try:
        assert await device.collect(settle=0.2) == []
    finally:
        await proxy.stop()
        device.close()


@async_test
async def test_idle_refresh_keeps_resending_the_last_frame():
    device = FakeDevice()
    proxy = Proxy(parse({
        "virtual_strip": {"led_count": 10},
        "inputs": {"ddp": {"port": free_port()}},
        "status": {"enabled": False},
        "output": {"idle_refresh_hz": 40},
        "targets": [{"name": "d", "host": "127.0.0.1", "port": device.port, "count": 10}],
    }))
    await proxy.start()
    try:
        packets = await device.collect(settle=0.25)
        assert len(packets) >= 3
        assert all(protocol.parse_ddp(p).data == b"\x00" * 30 for p in packets)
    finally:
        await proxy.stop()
        device.close()


@async_test
async def test_status_endpoint_reports_targets():
    device = FakeDevice()
    proxy = Proxy(parse({
        "virtual_strip": {"led_count": 10},
        "inputs": {"ddp": {"enabled": False}},
        "status": {"enabled": False},
        "targets": [{"name": "d", "host": "127.0.0.1", "port": device.port, "count": 10}],
    }))
    await proxy.start()
    port = free_port()
    server = StatusServer("127.0.0.1", port, proxy.snapshot)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /status.json HTTP/1.1\r\nHost: proxy\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), timeout=5)
        writer.close()
        head, _, body = raw.partition(b"\r\n\r\n")
        assert b"200 OK" in head
        payload = json.loads(body)
        assert payload["virtual_strip"]["led_count"] == 10
        assert payload["output"]["targets"][0]["name"] == "d"
    finally:
        await server.stop()
        await proxy.stop()
        device.close()
