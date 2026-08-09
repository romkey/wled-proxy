"""Command line entry point: ``python -m wled_proxy --config config.json``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import __version__, app
from . import config as config_module
from .outputs import describe_coverage

DEFAULT_CONFIG = os.environ.get("WLED_PROXY_CONFIG", "config.json")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wled-proxy",
        description="Fan a single large virtual LED strip out to any number of WLED devices.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG,
        help=f"path to the JSON config file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the config, print a summary and exit",
    )
    parser.add_argument(
        "--log-level",
        choices=config_module.LOG_LEVELS,
        help="override the log level from the config file",
    )
    parser.add_argument(
        "--version", action="version", version=f"wled-proxy {__version__}"
    )
    return parser.parse_args(argv)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    root = logging.getLogger("wled_proxy")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False


def print_check(config: config_module.Config) -> None:
    strip = config.strip
    targets = config.active_targets
    print(f"config : {config.source}")
    print(
        f"strip  : {strip.led_count} pixels, {strip.format.upper()}, "
        f"{strip.channels} channels"
    )
    enabled_inputs = [
        name
        for name, section in (
            ("ddp", config.inputs.ddp),
            ("artnet", config.inputs.artnet),
            ("e131", config.inputs.e131),
        )
        if section.enabled
    ]
    print(f"inputs : {', '.join(enabled_inputs) or 'none'}")
    print(
        f"targets: {len(targets)} enabled, "
        f"{len(config.targets) - len(targets)} disabled"
    )
    for target in targets:
        print(
            f"  {target.name:<20} {target.start:>6}-{target.end - 1:<6} "
            f"{target.protocol}/{target.format} -> {target.host}:{target.port}"
        )
    gaps, overlaps = describe_coverage(strip.led_count, targets)
    if gaps:
        print(f"warning: no target covers pixels {app.format_runs(gaps)}")
    if overlaps:
        print(
            f"note   : more than one target covers pixels {app.format_runs(overlaps)}"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level or "info")
    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.log_level:
        config.log_level = args.log_level
    setup_logging(config.log_level)

    if args.check:
        print_check(config)
        return 0

    try:
        return asyncio.run(app.run(config))
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        logging.getLogger("wled_proxy").error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
