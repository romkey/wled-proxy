import pytest

from wled_proxy.pixels import Canvas, PixelMapper, build_lut


def ramp(count, cpp=3):
    return bytes(range(count * cpp))


def test_canvas_writes_raw_channels():
    canvas = Canvas(10, "rgb")
    canvas.write(3, b"\x01\x02\x03", 3)
    assert canvas.buffer[3:6] == b"\x01\x02\x03"
    assert canvas.buffer[0:3] == b"\x00\x00\x00"


def test_canvas_clips_writes_past_the_end():
    canvas = Canvas(2, "rgb")
    written = canvas.write(3, b"\xff" * 12, 3)
    assert written == 3
    assert canvas.buffer == b"\x00\x00\x00\xff\xff\xff"


def test_canvas_converts_rgb_input_to_an_rgbw_strip():
    canvas = Canvas(2, "rgbw")
    canvas.write(0, bytes([1, 2, 3, 4, 5, 6]), 3)
    assert canvas.buffer == bytes([1, 2, 3, 0, 4, 5, 6, 0])


def test_canvas_converts_rgbw_input_to_an_rgb_strip():
    canvas = Canvas(2, "rgb")
    canvas.write(0, bytes([1, 2, 3, 99, 4, 5, 6, 99]), 4)
    assert canvas.buffer == bytes([1, 2, 3, 4, 5, 6])


def test_canvas_conversion_honours_the_offset():
    canvas = Canvas(3, "rgbw")
    canvas.write(3, bytes([7, 8, 9]), 3)  # second pixel of an RGB stream
    assert canvas.buffer[4:8] == bytes([7, 8, 9, 0])


def test_identity_mapping_is_a_plain_slice():
    canvas = Canvas(10, "rgb")
    canvas.buffer[:] = ramp(10)
    mapper = PixelMapper(source_format="rgb", start=2, count=3)
    assert mapper.render(canvas.view) == ramp(10)[6:15]


def test_reverse_flips_pixels_not_channels():
    canvas = Canvas(3, "rgb")
    canvas.buffer[:] = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])
    mapper = PixelMapper(source_format="rgb", start=0, count=3, reverse=True)
    assert mapper.render(canvas.view) == bytes([7, 8, 9, 4, 5, 6, 1, 2, 3])


def test_color_order_permutes_channels():
    canvas = Canvas(2, "rgb")
    canvas.buffer[:] = bytes([1, 2, 3, 4, 5, 6])
    mapper = PixelMapper(source_format="rgb", start=0, count=2, color_order="grb")
    assert mapper.render(canvas.view) == bytes([2, 1, 3, 5, 4, 6])


def test_rgb_source_to_rgbw_target_defaults_to_a_dark_white_channel():
    canvas = Canvas(2, "rgb")
    canvas.buffer[:] = bytes([10, 20, 30, 40, 50, 60])
    mapper = PixelMapper(source_format="rgb", start=0, count=2, target_format="rgbw")
    assert mapper.render(canvas.view) == bytes([10, 20, 30, 0, 40, 50, 60, 0])


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("brighter", bytes([90, 40, 60, 40])),
        ("accurate", bytes([50, 0, 20, 40])),
        ("max", bytes([90, 40, 60, 90])),
        ("luminance", bytes([90, 40, 60, 57])),
    ],
)
def test_white_modes(mode, expected):
    canvas = Canvas(1, "rgb")
    canvas.buffer[:] = bytes([90, 40, 60])
    mapper = PixelMapper(
        source_format="rgb", start=0, count=1, target_format="rgbw", white_mode=mode
    )
    assert mapper.render(canvas.view) == expected


def test_white_mode_respects_reverse_and_color_order():
    canvas = Canvas(2, "rgb")
    canvas.buffer[:] = bytes([90, 40, 60, 10, 20, 30])
    mapper = PixelMapper(
        source_format="rgb",
        start=0,
        count=2,
        reverse=True,
        target_format="rgbw",
        color_order="grbw",
        white_mode="brighter",
    )
    assert mapper.render(canvas.view) == bytes([20, 10, 30, 10, 40, 90, 60, 40])


def test_rgbw_source_to_rgb_target_adds_white_with_saturation():
    canvas = Canvas(2, "rgbw")
    canvas.buffer[:] = bytes([10, 20, 30, 5, 250, 250, 0, 20])
    mapper = PixelMapper(source_format="rgbw", start=0, count=2, target_format="rgb")
    assert mapper.render(canvas.view) == bytes([15, 25, 35, 255, 255, 20])


def test_rgbw_source_to_rgb_target_can_drop_white():
    canvas = Canvas(1, "rgbw")
    canvas.buffer[:] = bytes([10, 20, 30, 200])
    mapper = PixelMapper(
        source_format="rgbw", start=0, count=1, target_format="rgb", white_merge="drop"
    )
    assert mapper.render(canvas.view) == bytes([10, 20, 30])


def test_brightness_scales_every_channel():
    canvas = Canvas(1, "rgb")
    canvas.buffer[:] = bytes([100, 200, 255])
    mapper = PixelMapper(source_format="rgb", start=0, count=1, brightness=0.5)
    assert mapper.render(canvas.view) == bytes([50, 100, 128])


def test_gamma_curve_keeps_the_end_points():
    lut = build_lut(gamma=2.2)
    assert lut[0] == 0 and lut[255] == 255
    assert lut[128] < 128
    assert build_lut() is None


def test_mapper_rejects_a_color_order_that_is_not_a_permutation():
    with pytest.raises(ValueError):
        PixelMapper(source_format="rgb", start=0, count=1, color_order="rgbw")


def test_large_mapping_matches_a_naive_implementation():
    count = 1500
    canvas = Canvas(count, "rgb")
    canvas.buffer[:] = bytes((i * 7) % 256 for i in range(count * 3))
    mapper = PixelMapper(
        source_format="rgb", start=100, count=1000, reverse=True, color_order="bgr"
    )
    expected = bytearray()
    for i in reversed(range(100, 1100)):
        r, g, b = canvas.buffer[i * 3 : i * 3 + 3]
        expected += bytes([b, g, r])
    assert mapper.render(canvas.view) == bytes(expected)
