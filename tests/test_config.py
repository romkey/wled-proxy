import pathlib

import pytest

from wled_proxy import config as config_module
from wled_proxy.config import ConfigError, parse
from wled_proxy.outputs import describe_coverage


def base(**overrides):
    document = {
        "virtual_strip": {"led_count": 100},
        "targets": [{"host": "10.0.0.1", "count": 50}],
    }
    document.update(overrides)
    return document


def test_defaults():
    config = parse(base())
    assert config.strip.led_count == 100
    assert config.strip.format == "rgb"
    assert config.inputs.ddp.enabled is True
    assert config.inputs.ddp.port == 4048
    assert config.inputs.artnet.enabled is False
    assert config.output.max_fps == 60.0
    assert config.status.port == 8080

    target = config.targets[0]
    assert target.name == "10.0.0.1"
    assert target.protocol == "ddp"
    assert target.port == 4048
    assert target.start == 0
    assert target.count == 50
    assert target.color_order == "rgb"


def test_targets_chain_automatically_when_start_is_omitted():
    config = parse(
        base(
            targets=[
                {"host": "a", "count": 30},
                {"host": "b", "count": 20},
                {"host": "c", "count": 10, "start": 0},
                {"host": "d", "count": 5},
            ]
        )
    )
    spans = [(t.name, t.start, t.end) for t in config.targets]
    assert spans == [("a", 0, 30), ("b", 30, 50), ("c", 0, 10), ("d", 10, 15)]


def test_target_defaults_are_merged_into_every_target():
    config = parse(
        base(
            target_defaults={"protocol": "artnet", "format": "rgbw", "count": 25},
            targets=[{"host": "a"}, {"host": "b", "protocol": "ddp"}],
        )
    )
    assert [t.protocol for t in config.targets] == ["artnet", "ddp"]
    assert all(t.format == "rgbw" and t.count == 25 for t in config.targets)
    assert config.targets[0].port == 6454
    assert config.targets[1].port == 4048


def test_default_universe_size_follows_the_pixel_format():
    config = parse(
        base(
            targets=[
                {"host": "a", "count": 10, "protocol": "artnet"},
                {"host": "b", "count": 10, "protocol": "artnet", "format": "rgbw"},
            ]
        )
    )
    assert config.targets[0].channels_per_universe == 510
    assert config.targets[1].channels_per_universe == 512
    assert config.targets[0].universe == 0


@pytest.mark.parametrize(
    "document,message",
    [
        (
            base(targets=[{"host": "a", "count": 50, "start": 60}]),
            "virtual strip is only",
        ),
        (base(targets=[{"host": "a", "count": 0}]), "at least 1"),
        (base(targets=[{"host": "a", "count": 10, "protocol": "sacn"}]), "not one of"),
        (
            base(targets=[{"host": "a", "count": 10, "colour_order": "grb"}]),
            "unknown option",
        ),
        (base(targets=[{"count": 10}]), "host is required"),
        (
            base(targets=[{"host": "a", "count": 10, "color_order": "xyz"}]),
            "not one of",
        ),
        (
            base(targets=[{"host": "a", "count": 10}, {"host": "a", "count": 10}]),
            "duplicate name",
        ),
        (base(virtual_strip={"led_count": 100, "format": "rgbww"}), "not one of"),
        (base(virtual_strip={}), "led_count is required"),
        (base(unexpected=1), "unknown option"),
        (base(targets=[{"host": "a", "count": 10, "brightness": 4}]), "at most 1"),
        (
            base(targets=[{"host": "a", "count": 10, "enabled": "yes"}]),
            "expected true or false",
        ),
        (base(targets=[{"host": "a", "count": "10"}]), "expected an integer"),
        ({"virtual_strip": {"led_count": 10}, "targets": {}}, "expected a list"),
    ],
)
def test_rejects_bad_configuration(document, message):
    with pytest.raises(ConfigError) as error:
        parse(document)
    assert message in str(error.value)


def test_multicast_output_is_rejected_for_non_sacn_targets():
    with pytest.raises(ConfigError) as error:
        parse(base(targets=[{"host": "a", "count": 10, "multicast": True}]))
    assert "only available for the e131 protocol" in str(error.value)
    parse(base(targets=[{"host": "a", "count": 10, "protocol": "e131", "multicast": True}]))


def test_artnet_universe_size_must_hold_whole_pixels():
    with pytest.raises(ConfigError) as error:
        parse(
            base(
                targets=[
                    {
                        "host": "a",
                        "count": 10,
                        "protocol": "artnet",
                        "channels_per_universe": 500,
                    }
                ]
            )
        )
    assert "multiple of 3" in str(error.value)


def test_load_reads_a_file_with_comments(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("""
        // a whole line
        {
          "virtual_strip": { "led_count": 100 },   // and a trailing one
          "targets": [
            { "host": "10.0.0.1", "count": 50, "name": "a // b" }
          ]
        }
    """)
    config = config_module.load(path)
    assert config.strip.led_count == 100
    assert config.targets[0].name == "a // b"
    assert config.source == str(path)


def test_comment_stripping_preserves_line_numbers(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('// one\n// two\n{\n  "virtual_strip": [\n}\n')
    with pytest.raises(ConfigError) as error:
        config_module.load(path)
    assert "line 5" in str(error.value)


def test_all_shipped_configs_are_valid():
    root = pathlib.Path(__file__).resolve().parent.parent
    paths = [root / "config.json", *sorted((root / "examples").glob("*.json"))]
    assert len(paths) >= 3
    for path in paths:
        config = config_module.load(path)
        assert config.targets, f"{path.name} has no targets"


def test_load_reports_invalid_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{\n  "virtual_strip": {,}\n}')
    with pytest.raises(ConfigError) as error:
        config_module.load(path)
    assert "invalid JSON at line" in str(error.value)


def test_coverage_finds_gaps_and_overlaps():
    config = parse(
        base(
            targets=[
                {"host": "a", "count": 10, "start": 0},
                {"host": "b", "count": 10, "start": 5},
                {"host": "c", "count": 10, "start": 90},
            ]
        )
    )
    gaps, overlaps = describe_coverage(100, config.active_targets)
    assert gaps == [(15, 89)]
    assert overlaps == [(5, 9)]
