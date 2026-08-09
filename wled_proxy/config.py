"""JSON configuration loading and validation.

Unknown keys are rejected rather than ignored: a typo in a config file is far
easier to find at startup than by wondering why a strip stayed dark.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pixels import FORMATS, WHITE_MERGES, WHITE_MODES, color_orders
from .protocol import (
    ARTNET_CHANNELS_RGB,
    ARTNET_CHANNELS_RGBW,
    ARTNET_PORT,
    DDP_PORT,
    E131_DEFAULT_PRIORITY,
    E131_PORT,
)

PROTOCOLS = ("ddp", "artnet", "e131")
LOG_LEVELS = ("debug", "info", "warning", "error")

DEFAULT_PORTS = {
    "ddp": DDP_PORT,
    "artnet": ARTNET_PORT,
    "e131": E131_PORT,
}

_MISSING = object()


class ConfigError(Exception):
    """Raised for any malformed or inconsistent configuration."""


class Section:
    """A dict in the config file, read key by key with type checking."""

    def __init__(self, raw: Any, path: str = ""):
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path or 'config'}: expected an object, got {_typename(raw)}")
        self._raw = dict(raw)
        self._path = path

    def _where(self, key: str) -> str:
        return f"{self._path}.{key}" if self._path else key

    def _take(self, key: str, default: Any) -> Any:
        value = self._raw.pop(key, _MISSING)
        if value is _MISSING or value is None:
            if default is _MISSING:
                raise ConfigError(f"{self._where(key)} is required")
            return default
        return value

    def string(self, key, default=_MISSING, *, choices=None, lower=True) -> Any:
        value = self._take(key, default)
        if not isinstance(value, str):
            raise ConfigError(f"{self._where(key)}: expected a string, got {_typename(value)}")
        if lower:
            value = value.strip().lower()
        if choices and value not in choices:
            raise ConfigError(
                f"{self._where(key)}: {value!r} is not one of {', '.join(map(str, choices))}"
            )
        return value

    def integer(self, key, default=_MISSING, *, minimum=None, maximum=None) -> Any:
        value = self._take(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{self._where(key)}: expected an integer, got {_typename(value)}")
        if minimum is not None and value < minimum:
            raise ConfigError(f"{self._where(key)}: must be at least {minimum}, got {value}")
        if maximum is not None and value > maximum:
            raise ConfigError(f"{self._where(key)}: must be at most {maximum}, got {value}")
        return value

    def number(self, key, default=_MISSING, *, minimum=None, maximum=None) -> Any:
        value = self._take(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{self._where(key)}: expected a number, got {_typename(value)}")
        value = float(value)
        if minimum is not None and value < minimum:
            raise ConfigError(f"{self._where(key)}: must be at least {minimum}, got {value}")
        if maximum is not None and value > maximum:
            raise ConfigError(f"{self._where(key)}: must be at most {maximum}, got {value}")
        return value

    def boolean(self, key, default=_MISSING) -> Any:
        value = self._take(key, default)
        if not isinstance(value, bool):
            raise ConfigError(f"{self._where(key)}: expected true or false, got {_typename(value)}")
        return value

    def section(self, key) -> Section:
        return Section(self._raw.pop(key, None), self._where(key))

    def array(self, key, default=_MISSING) -> list:
        value = self._take(key, default)
        if not isinstance(value, list):
            raise ConfigError(f"{self._where(key)}: expected a list, got {_typename(value)}")
        return value

    def raw_dict(self, key) -> dict:
        value = self._raw.pop(key, None) or {}
        if not isinstance(value, dict):
            raise ConfigError(f"{self._where(key)}: expected an object, got {_typename(value)}")
        return value

    def finish(self) -> None:
        if self._raw:
            unknown = ", ".join(sorted(self._raw))
            where = self._path or "config"
            raise ConfigError(f"{where}: unknown option(s): {unknown}")


def _typename(value: Any) -> str:
    return {
        dict: "an object",
        list: "a list",
        str: "a string",
        bool: "a boolean",
        int: "a number",
        float: "a number",
        type(None): "null",
    }.get(type(value), type(value).__name__)


@dataclass
class StripConfig:
    led_count: int = 300
    format: str = "rgb"

    @property
    def channels(self) -> int:
        return self.led_count * len(self.format)


@dataclass
class DDPInputConfig:
    enabled: bool = True
    bind: str = "0.0.0.0"
    port: int = DDP_PORT
    format: str = "rgb"
    channel_offset: int = 0


@dataclass
class ArtnetInputConfig:
    enabled: bool = False
    bind: str = "0.0.0.0"
    port: int = ARTNET_PORT
    format: str = "rgb"
    start_universe: int = 0
    channels_per_universe: int = 0
    dmx_start_channel: int = 1

    def __post_init__(self):
        self.channels_per_universe = self.channels_per_universe or _default_universe_size(
            self.format
        )


@dataclass
class E131InputConfig:
    enabled: bool = False
    bind: str = "0.0.0.0"
    port: int = E131_PORT
    format: str = "rgb"
    start_universe: int = 1
    channels_per_universe: int = 0
    dmx_start_channel: int = 1
    multicast: bool = True
    multicast_interface: str = "0.0.0.0"
    min_priority: int = 0

    def __post_init__(self):
        self.channels_per_universe = self.channels_per_universe or _default_universe_size(
            self.format
        )


@dataclass
class InputsConfig:
    ddp: DDPInputConfig = field(default_factory=DDPInputConfig)
    artnet: ArtnetInputConfig = field(default_factory=ArtnetInputConfig)
    e131: E131InputConfig = field(default_factory=E131InputConfig)


@dataclass
class OutputConfig:
    max_fps: float = 60.0
    frame_timeout_ms: float = 15.0
    idle_refresh_hz: float = 0.0
    resolve_interval_s: float = 300.0


@dataclass
class StatusConfig:
    enabled: bool = True
    bind: str = "0.0.0.0"
    port: int = 8080


@dataclass
class TargetConfig:
    name: str
    host: str
    protocol: str = "ddp"
    enabled: bool = True
    port: int = DDP_PORT
    format: str = "rgb"
    start: int = 0
    count: int = 0
    reverse: bool = False
    color_order: str = ""
    brightness: float = 1.0
    gamma: float = 1.0
    white_mode: str = "none"
    white_merge: str = "add"
    remote_start: int = 0
    universe: int = 0
    channels_per_universe: int = 0
    priority: int = E131_DEFAULT_PRIORITY
    multicast: bool = False
    multicast_ttl: int = 1
    source_name: str = "WLED Proxy"

    def __post_init__(self):
        self.color_order = self.color_order or self.format
        self.channels_per_universe = self.channels_per_universe or _default_universe_size(
            self.format
        )

    @property
    def channels_per_pixel(self) -> int:
        return len(self.format)

    @property
    def end(self) -> int:
        return self.start + self.count


@dataclass
class Config:
    strip: StripConfig = field(default_factory=StripConfig)
    inputs: InputsConfig = field(default_factory=InputsConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    status: StatusConfig = field(default_factory=StatusConfig)
    targets: list[TargetConfig] = field(default_factory=list)
    log_level: str = "info"
    source: str = "<none>"

    @property
    def active_targets(self) -> list[TargetConfig]:
        return [t for t in self.targets if t.enabled]


def load(path: str | Path) -> Config:
    """Read and validate a config file."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        raw = json.loads(_strip_comments(text))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc
    config = parse(raw)
    config.source = str(path)
    return config


def _strip_comments(text: str) -> str:
    """Drop ``//`` comments so configs can be annotated in place.

    Newlines are kept so that line numbers in JSON errors still point at the
    original file, and ``//`` inside a string is left alone.
    """
    out = []
    in_string = escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "/" and text[index + 1 : index + 2] == "/":
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse(raw: Any) -> Config:
    root = Section(raw, "")
    config = Config()
    config.log_level = root.string("log_level", "info", choices=LOG_LEVELS)

    strip = root.section("virtual_strip")
    config.strip = StripConfig(
        led_count=strip.integer("led_count", minimum=1, maximum=1_000_000),
        format=strip.string("format", "rgb", choices=FORMATS),
    )
    strip.finish()

    config.inputs = _parse_inputs(root.section("inputs"), config.strip)
    config.output = _parse_output(root.section("output"))
    config.status = _parse_status(root.section("status"))

    defaults = root.raw_dict("target_defaults")
    entries = root.array("targets", [])
    root.finish()

    config.targets = _parse_targets(entries, defaults, config.strip)
    return config


def _parse_inputs(section: Section, strip: StripConfig) -> InputsConfig:
    inputs = InputsConfig()

    ddp = section.section("ddp")
    inputs.ddp = DDPInputConfig(
        enabled=ddp.boolean("enabled", True),
        bind=ddp.string("bind", "0.0.0.0", lower=False),
        port=ddp.integer("port", DDP_PORT, minimum=1, maximum=65535),
        format=ddp.string("format", strip.format, choices=FORMATS),
        channel_offset=ddp.integer("channel_offset", 0, minimum=0),
    )
    ddp.finish()

    art = section.section("artnet")
    art_format = art.string("format", strip.format, choices=FORMATS)
    inputs.artnet = ArtnetInputConfig(
        enabled=art.boolean("enabled", False),
        bind=art.string("bind", "0.0.0.0", lower=False),
        port=art.integer("port", ARTNET_PORT, minimum=1, maximum=65535),
        format=art_format,
        start_universe=art.integer("start_universe", 0, minimum=0, maximum=32767),
        channels_per_universe=art.integer(
            "channels_per_universe",
            _default_universe_size(art_format),
            minimum=1,
            maximum=512,
        ),
        dmx_start_channel=art.integer("dmx_start_channel", 1, minimum=1, maximum=512),
    )
    art.finish()

    e131 = section.section("e131")
    e131_format = e131.string("format", strip.format, choices=FORMATS)
    inputs.e131 = E131InputConfig(
        enabled=e131.boolean("enabled", False),
        bind=e131.string("bind", "0.0.0.0", lower=False),
        port=e131.integer("port", E131_PORT, minimum=1, maximum=65535),
        format=e131_format,
        start_universe=e131.integer("start_universe", 1, minimum=1, maximum=63999),
        channels_per_universe=e131.integer(
            "channels_per_universe",
            _default_universe_size(e131_format),
            minimum=1,
            maximum=512,
        ),
        dmx_start_channel=e131.integer("dmx_start_channel", 1, minimum=1, maximum=512),
        multicast=e131.boolean("multicast", True),
        multicast_interface=e131.string("multicast_interface", "0.0.0.0", lower=False),
        min_priority=e131.integer("min_priority", 0, minimum=0, maximum=200),
    )
    e131.finish()

    section.finish()
    return inputs


def _parse_output(section: Section) -> OutputConfig:
    output = OutputConfig(
        max_fps=section.number("max_fps", 60.0, minimum=0.1, maximum=1000.0),
        frame_timeout_ms=section.number("frame_timeout_ms", 15.0, minimum=1.0, maximum=5000.0),
        idle_refresh_hz=section.number("idle_refresh_hz", 0.0, minimum=0.0, maximum=100.0),
        resolve_interval_s=section.number("resolve_interval_s", 300.0, minimum=0.0),
    )
    section.finish()
    return output


def _parse_status(section: Section) -> StatusConfig:
    status = StatusConfig(
        enabled=section.boolean("enabled", True),
        bind=section.string("bind", "0.0.0.0", lower=False),
        port=section.integer("port", 8080, minimum=1, maximum=65535),
    )
    section.finish()
    return status


def _default_universe_size(fmt: str) -> int:
    return ARTNET_CHANNELS_RGBW if fmt == "rgbw" else ARTNET_CHANNELS_RGB


def _parse_targets(entries: list, defaults: dict, strip: StripConfig) -> list[TargetConfig]:
    targets: list[TargetConfig] = []
    names: set[str] = set()
    cursor = 0

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"targets[{index}]: expected an object, got {_typename(entry)}")
        merged = {**defaults, **entry}
        section = Section(merged, f"targets[{index}]")

        host = section.string("host", lower=False)
        name = section.string("name", host, lower=False)
        if name in names:
            raise ConfigError(f"targets[{index}]: duplicate name {name!r}")
        names.add(name)

        where = f"targets[{index}] ({name})"
        proto = section.string("protocol", "ddp", choices=PROTOCOLS)
        fmt = section.string("format", "rgb", choices=FORMATS)
        count = section.integer("count", minimum=1, maximum=strip.led_count)
        start = section.integer("start", cursor, minimum=0, maximum=strip.led_count - 1)
        order = section.string("color_order", fmt, choices=color_orders(fmt))

        target = TargetConfig(
            name=name,
            host=host,
            protocol=proto,
            enabled=section.boolean("enabled", True),
            port=section.integer("port", DEFAULT_PORTS[proto], minimum=1, maximum=65535),
            format=fmt,
            start=start,
            count=count,
            reverse=section.boolean("reverse", False),
            color_order=order,
            brightness=section.number("brightness", 1.0, minimum=0.0, maximum=1.0),
            gamma=section.number("gamma", 1.0, minimum=0.1, maximum=5.0),
            white_mode=section.string("white_mode", "none", choices=WHITE_MODES),
            white_merge=section.string("white_merge", "add", choices=WHITE_MERGES),
            remote_start=section.integer("remote_start", 0, minimum=0),
            universe=section.integer(
                "universe", 0 if proto == "artnet" else 1, minimum=0, maximum=63999
            ),
            channels_per_universe=section.integer(
                "channels_per_universe",
                _default_universe_size(fmt),
                minimum=1,
                maximum=512,
            ),
            priority=section.integer("priority", E131_DEFAULT_PRIORITY, minimum=0, maximum=200),
            multicast=section.boolean("multicast", False),
            multicast_ttl=section.integer("multicast_ttl", 1, minimum=1, maximum=255),
            source_name=section.string("source_name", "WLED Proxy", lower=False),
        )
        section.finish()

        if target.end > strip.led_count:
            raise ConfigError(
                f"{where}: covers pixels {target.start}-{target.end - 1} but the virtual "
                f"strip is only {strip.led_count} pixels long"
            )
        if (
            target.protocol in ("artnet", "e131")
            and target.channels_per_universe % target.channels_per_pixel
        ):
            raise ConfigError(
                f"{where}: channels_per_universe ({target.channels_per_universe}) must be a "
                f"multiple of {target.channels_per_pixel} for {target.format} pixels"
            )
        if target.multicast and target.protocol != "e131":
            raise ConfigError(f"{where}: multicast output is only available for the e131 protocol")
        if len(target.source_name.encode("utf-8")) > 63:
            raise ConfigError(f"{where}: source_name must be at most 63 bytes")

        targets.append(target)
        cursor = target.end

    return targets
