"""The virtual strip buffer and the per-target pixel mapping.

Everything here works on plain ``bytearray``/``memoryview`` objects and leans
on CPython's C level operations - contiguous slices, strided slice assignment,
``operator.itemgetter`` gathers and ``bytes.translate`` lookup tables - so a
few thousand pixels at 60 fps costs very little CPU and the proxy needs no
third party dependencies.
"""

from __future__ import annotations

import operator
from itertools import permutations

FORMATS = ("rgb", "rgbw")
WHITE_MODES = ("none", "brighter", "accurate", "max", "luminance")
WHITE_MERGES = ("add", "drop")


def color_orders(fmt: str) -> tuple[str, ...]:
    """Every accepted channel order for a format, e.g. ``grb`` for ``rgb``."""
    return tuple("".join(p) for p in permutations(fmt))


class Canvas:
    """The virtual LED strip that inputs write into and targets read from."""

    def __init__(self, led_count: int, fmt: str = "rgb"):
        if fmt not in FORMATS:
            raise ValueError(f"unsupported format {fmt!r}")
        self.led_count = led_count
        self.format = fmt
        self.channels_per_pixel = len(fmt)
        self.buffer = bytearray(led_count * self.channels_per_pixel)
        self.view = memoryview(self.buffer)

    def __len__(self) -> int:
        return len(self.buffer)

    def clear(self) -> None:
        self.buffer[:] = bytes(len(self.buffer))

    def write(self, channel_offset: int, data, source_channels: int) -> int:
        """Write raw channel data at ``channel_offset``, converting the pixel
        format if the source does not match the canvas. Data past the end of
        the strip is discarded. Returns the number of channels stored.
        """
        if channel_offset < 0:
            return 0
        cpp = self.channels_per_pixel
        if source_channels == cpp:
            end = min(channel_offset + len(data), len(self.buffer))
            if end <= channel_offset:
                return 0
            n = end - channel_offset
            self.buffer[channel_offset:end] = data[:n]
            return n
        return self._write_converted(channel_offset, data, source_channels)

    def _write_converted(self, channel_offset: int, data, source_channels: int) -> int:
        cpp = self.channels_per_pixel
        # Only whole pixels can be converted, so round the destination up to a
        # pixel boundary and drop any leading partial pixel from the source.
        pixel, remainder = divmod(channel_offset, source_channels)
        skip = 0
        if remainder:
            pixel += 1
            skip = source_channels - remainder
        pixels = (len(data) - skip) // source_channels
        pixels = min(pixels, self.led_count - pixel)
        if pixels <= 0:
            return 0

        src = memoryview(data)[skip : skip + pixels * source_channels]
        start = pixel * cpp
        dst = self.view[start : start + pixels * cpp]
        for i in range(min(source_channels, cpp)):
            dst[i::cpp] = src[i::source_channels].tobytes()
        if cpp > source_channels:
            dst[source_channels::cpp] = bytes(pixels)
        return pixels * cpp


def build_lut(brightness: float = 1.0, gamma: float = 1.0) -> bytes | None:
    """A 256 entry translation table, or None when it would be the identity."""
    if brightness == 1.0 and gamma == 1.0:
        return None
    table = bytearray(256)
    for i in range(256):
        value = (i / 255.0) ** gamma * brightness * 255.0
        table[i] = min(255, max(0, round(value)))
    return bytes(table)


def _add_saturating(a: int, b: int) -> int:
    total = a + b
    return min(total, 255)


class PixelMapper:
    """Renders one slice of the canvas into a single target's wire format."""

    def __init__(
        self,
        *,
        source_format: str,
        start: int,
        count: int,
        reverse: bool = False,
        target_format: str = "rgb",
        color_order: str | None = None,
        white_mode: str = "none",
        white_merge: str = "add",
        brightness: float = 1.0,
        gamma: float = 1.0,
    ):
        self.source_format = source_format
        self.target_format = target_format
        self.start = start
        self.count = count
        self.reverse = reverse
        self.color_order = color_order or target_format
        self.white_mode = white_mode
        self.white_merge = white_merge

        if self.source_format not in FORMATS or self.target_format not in FORMATS:
            raise ValueError("format must be one of " + ", ".join(FORMATS))
        if sorted(self.color_order) != sorted(self.target_format):
            raise ValueError(
                f"color_order {self.color_order!r} is not a permutation of {self.target_format!r}"
            )

        self.src_cpp = len(self.source_format)
        self.dst_cpp = len(self.target_format)
        self.byte_count = count * self.dst_cpp
        self._first = start * self.src_cpp
        self._last = (start + count) * self.src_cpp
        self._lut = build_lut(brightness, gamma)

        adds_white = self.src_cpp == 3 and self.dst_cpp == 4 and white_mode != "none"
        merges_white = self.src_cpp == 4 and self.dst_cpp == 3 and white_merge == "add"
        self._needs_arithmetic = adds_white or merges_white

        self._identity = (
            not self._needs_arithmetic
            and not reverse
            and self.source_format == self.target_format
            and self.color_order == self.target_format
        )

        self._gather = None
        self._scratch = None
        if not self._identity and not self._needs_arithmetic:
            self._gather = operator.itemgetter(*self._gather_indices())
            # One trailing zero byte the gather can point at for channels the
            # source does not have (a white channel fed from RGB data).
            self._scratch = bytearray(self._last - self._first + 1)

    def _gather_indices(self) -> list[int]:
        zero = self._last - self._first
        indices = []
        for i in range(self.count):
            pixel = (self.count - 1 - i) if self.reverse else i
            base = pixel * self.src_cpp
            for channel in self.color_order:
                position = self.source_format.find(channel)
                indices.append(zero if position < 0 else base + position)
        return indices

    def render(self, view: memoryview) -> bytes | bytearray:
        """Render this target's pixels from a canvas memoryview."""
        if self._identity:
            out = view[self._first : self._last].tobytes()
        elif self._gather is not None:
            scratch = self._scratch
            scratch[:-1] = view[self._first : self._last]
            out = bytes(self._gather(scratch))
        else:
            out = self._render_arithmetic(view)
        return out.translate(self._lut) if self._lut is not None else out

    def _render_arithmetic(self, view: memoryview) -> bytearray:
        src = view[self._first : self._last]
        channels = {}
        for i, name in enumerate(self.source_format):
            plane = src[i :: self.src_cpp].tobytes()
            channels[name] = plane[::-1] if self.reverse else plane

        if self.src_cpp == 3:
            r, g, b = channels["r"], channels["g"], channels["b"]
            mode = self.white_mode
            if mode == "max":
                w = bytes(map(max, r, g, b))
            elif mode == "luminance":
                w = bytes(
                    (rv * 77 + gv * 150 + bv * 29) >> 8 for rv, gv, bv in zip(r, g, b, strict=True)
                )
            else:
                w = bytes(map(min, r, g, b))
                if mode == "accurate":
                    r = bytes(map(operator.sub, r, w))
                    g = bytes(map(operator.sub, g, w))
                    b = bytes(map(operator.sub, b, w))
            channels = {"r": r, "g": g, "b": b, "w": w}
        else:
            w = channels["w"]
            channels = {name: bytes(map(_add_saturating, channels[name], w)) for name in "rgb"}

        out = bytearray(self.byte_count)
        for i, name in enumerate(self.color_order):
            out[i :: self.dst_cpp] = channels[name]
        return out
