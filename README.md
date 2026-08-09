# wled-proxy

[![CI](https://github.com/romkey/wled-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/romkey/wled-proxy/actions/workflows/ci.yml)
[![Lint](https://github.com/romkey/wled-proxy/actions/workflows/lint.yml/badge.svg)](https://github.com/romkey/wled-proxy/actions/workflows/lint.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Present any number of WLED devices as a **single large virtual LED strip**.

WLED can already drive remote strips over the network, but it caps you at ten
of them, and chaining ESP32s together to get past that leaves you with a pile
of microcontrollers whose only job is to forward packets. This proxy does the
same job on a Linux box, with no limit on how many devices you attach, and
nothing to reflash when the layout changes.

Upstream, it looks like one ordinary realtime LED device. Downstream, it slices
that strip up and forwards each piece to a real controller.

```
   xLights / WLED master / Hyperion / LedFx / your own script
                            |
                  DDP, Art-Net or E1.31
                            |
                    +---------------+
                    |  wled-proxy   |   one 6980 pixel virtual strip
                    +---------------+
        ______________|_____|_____|______________
       |              |     |     |              |
   pixels 0-299   300-599  600-899   ...   6680-6979
   10.10.20.11  10.10.20.12  10.10.20.13   10.10.20.30
     (DDP)        (DDP)       (Art-Net)      (DDP)
```

- **Inputs:** DDP (RGB and RGBW), Art-Net (RGB and RGBW), E1.31 / sACN, with
  multicast.
- **Outputs:** the same three protocols, mixed freely, one choice per target.
- **Configuration:** a single JSON file.
- **Deployment:** `docker compose up -d`.
- **No dependencies.** Pure Python standard library, so it also runs directly
  on anything with Python 3.10 or newer, a Raspberry Pi included.

Every wire format here is byte-compatible with WLED's own implementation,
because the constants and framing were taken from WLED's `udp.cpp`, `e131.cpp`
and `ESPAsyncE131.h`.

## Quick start

```bash
git clone <this repo> wled-proxy && cd wled-proxy
$EDITOR config.json          # list your devices
docker compose up -d
```

Then open `http://<host>:8080/` for a live view of what is flowing through.

Before wiring up a real show, prove the path end to end with the built-in test
pattern, which sends straight at the proxy:

```bash
docker compose exec wled-proxy python -m wled_proxy.testpattern \
    --host 127.0.0.1 --count 900 --pattern index
```

`--pattern index` lights every 10th pixel green and every 50th red, which makes
it obvious at a glance whether your strips are in the right order and facing
the right way. `sweep` runs a single white pixel along the whole strip, which
is the fastest way to find a device that is out of position.

## Configuring it

A minimal config, three controllers of 300 pixels each:

```json
{
  "virtual_strip": { "led_count": 900 },
  "inputs": { "ddp": { "enabled": true } },
  "targets": [
    { "name": "bench-left",  "host": "10.0.0.51", "count": 300 },
    { "name": "bench-right", "host": "10.0.0.52", "count": 300 },
    { "name": "ceiling",     "host": "10.0.0.53", "count": 300, "reverse": true }
  ]
}
```

Leaving `start` out is the point: each target picks up where the previous one
ended, so the order of the list *is* the physical order of the strip. Move an
entry and that section of the show moves with it. Set `start` explicitly when
you want a device to mirror a section rather than extend the strip.

The options you will actually reach for:

| Option | Applies to | What it does |
| --- | --- | --- |
| `host` | every target | IP address or host name of the device |
| `count` | every target | how many virtual pixels this device shows |
| `start` | every target | first virtual pixel; defaults to chaining |
| `reverse` | every target | device is wired against the strip direction |
| `protocol` | every target | `ddp` (default), `artnet` or `e131` |
| `format` | every target | `rgb` or `rgbw`, matching the device's bus |
| `brightness` | every target | 0.0–1.0 cap for one device |
| `white_mode` | RGBW targets | synthesise white from an RGB source |
| `universe` | Art-Net, E1.31 | first universe to send on |
| `multicast` | E1.31 | send to `239.255.x.x` instead of unicast |

`target_defaults` holds anything you would otherwise repeat on every entry.
[`examples/annotated.json`](examples/annotated.json) lists every option with
its default and a note on what it is for, and
[`examples/hackerspace.json`](examples/hackerspace.json) is a worked
two-dozen-controller layout. `//` line comments are allowed in config files.

Check a config without starting anything:

```bash
python -m wled_proxy --config config.json --check
```

That prints the resulting pixel map and warns about stretches of the virtual
strip that no target covers, which is almost always a miscounted `count`.

## Feeding it

The proxy is a realtime target like any WLED, so anything that can drive a
WLED can drive it.

**From a master WLED.** In the master's *LED Preferences*, add a bus of type
**DDP RGB**, set its IP to the proxy and its length to the full virtual strip
length. The master then runs effects across the whole thing and the proxy
distributes them. Note that the master still allocates a pixel buffer for the
entire strip, so a big installation can outgrow an ESP32; if that happens,
drive the proxy from a computer instead.

**From xLights, Hyperion, LedFx, Falcon Player or a script.** Point them at
the proxy's IP and treat it as one long strip. Nothing else is special about
it.

Whichever you use, `--check` and the test pattern make it easy to confirm the
proxy sees frames before you go looking for problems further down the chain.

## Which protocol to use downstream

**Use DDP for WLED targets** unless you have a reason not to. It carries a
byte offset in every packet, so there is no universe bookkeeping and no limit
on strip length, and it is the protocol WLED itself uses for remote strips.

Art-Net and E1.31 are there for gear that speaks nothing else. Two things to
know if you use them with WLED targets:

- WLED only listens to a fixed number of universes: 9 on ESP8266, 12 to 20 on
  ESP32 depending on the build. That works out to roughly 1500 to 3400 RGB
  pixels per device, where DDP has no ceiling.
- The device's *start universe* must match the target's `universe` setting.
  WLED's own Art-Net output starts at universe 0 while its E1.31 default is 1,
  so the proxy defaults the same way, but check the device's sync settings.

The proxy always packs 510 channels per universe for RGB and 512 for RGBW,
which is what WLED's receiver assumes when it works out which pixel a universe
starts at. Change `channels_per_universe` only for non-WLED gear that needs
something else.

Also worth knowing: WLED can *send* DDP and Art-Net but not E1.31, its E1.31
output being an empty stub. The proxy sends all three.

## Colour handling

By default the proxy is a pipe: pixels arrive, get sliced, and leave untouched.
Leave `color_order` alone for WLED targets, since WLED applies its own colour
order on the device and doing it twice will undo it.

The knobs are for the awkward devices in the corner. `brightness` caps one
device that is blinding compared to the rest. `gamma` fixes a strip whose curve
does not match its neighbours. `white_mode` fills in the white channel when an
RGB show reaches an RGBW strip: `brighter` adds white on top of the colour,
`accurate` moves the common component into the white channel instead,
`luminance` and `max` are variations worth trying on strips where those two
look wrong. Going the other way, an RGBW source reaching an RGB device folds
white back into the colour channels unless you set `white_merge` to `drop`.

## Timing

Inputs write into the virtual strip as packets arrive. A frame is forwarded
when the sender says it is complete — a DDP push flag, an Art-Net sync, or the
last expected universe — so downstream devices never see half of one frame and
half of the next. Senders that never signal a frame boundary are covered by
`frame_timeout_ms`, after which whatever arrived is sent anyway.

`max_fps` (default 60) caps how often frames go out; a source pushing 200 fps
is coalesced rather than passed straight through to your network. If a device
drops out of realtime mode during quiet passages, set `idle_refresh_hz` to
resend the last frame periodically. WLED reverts to its own effects about 2.5
seconds after realtime data stops, which is usually what you want.

## Operating it

The status page at `http://<host>:8080/` shows input and output frame rates,
per-target packet and byte counts, and any send errors. `status.json` at the
same host serves the raw numbers, and `/healthz` is a plain health check, which
is what the compose file's `healthcheck` polls. `docker compose ps` will show
the container as `healthy` once the proxy is answering. If you turn the status
server off in the config, drop the `healthcheck` block too, or the container
will be reported unhealthy while working perfectly well.

Reload the config without dropping the process:

```bash
docker compose kill -s HUP wled-proxy
```

A config that fails to parse is rejected and the running one stays in place,
with the reason logged. A good one rebinds the inputs and rebuilds the targets,
so you can add devices to a running installation.

## Networking

The shipped compose file uses `network_mode: host`. It is the simplest option,
it keeps UDP latency down, and it is the only mode where sACN multicast input
works with no extra setup. Use it if you can.

If host networking is off the table — a managed Docker platform, a host whose
port 4048 is already taken, or a policy against it — you have two options.

**Bridge networking with published ports** is the easy one, and it is enough
if every source sends to the proxy by unicast. Comment out `network_mode` in
the compose file and uncomment the `ports` block. What you give up is anything
that depends on the proxy being a real presence on the LAN: sACN multicast
input will not reach it, Art-Net broadcast will not reach it, and devices see
the traffic arriving from the Docker host's address rather than a distinct one.
Outbound unicast to your WLED devices is unaffected, so a DDP-in, DDP-out
installation runs fine this way.

**macvlan or ipvlan** gives the container its own IP address on your LAN, at
which point everything works exactly as it does under host networking, without
the container sharing the host's network stack. Multicast, broadcast and mDNS
all behave normally. [`examples/docker-compose.macvlan.yml`](examples/docker-compose.macvlan.yml)
is a ready-to-edit version; the essentials are:

```yaml
networks:
  leds:
    driver: macvlan          # or: ipvlan
    driver_opts:
      parent: eno1           # host NIC on the LED VLAN
      # ipvlan_mode: l2      # ipvlan only
    ipam:
      config:
        - subnet: 10.10.20.0/24
          gateway: 10.10.20.1
          ip_range: 10.10.20.240/28
```

Four things to get right:

- **Pick addresses outside your DHCP pool.** Docker's IPAM assigns these
  statically and never asks a DHCP server, so an overlapping pool will hand
  the same address to something else eventually.
- **On WiFi, use ipvlan, not macvlan.** Access points drop frames from MAC
  addresses they did not authenticate, and macvlan invents a new MAC per
  container. ipvlan in L2 mode shares the host's MAC and gets through.
- **For a tagged VLAN,** create the sub-interface on the host first
  (`ip link add link eno1 name eno1.20 type vlan id 20`) and point `parent` at
  it.
- **The Docker host cannot reach the container** through the parent NIC. This
  catches everyone once. It is a kernel restriction on macvlan and ipvlan, not
  a Docker bug: the rest of the LAN reaches the proxy fine, but the host it
  runs on cannot, so the status page appears dead from exactly the machine you
  are likely to be logged into.

That last point is fixable with a shim interface on the host, if you want the
status page or a local Grafana to reach the proxy:

```bash
sudo ip link add leds-shim link eno1 type macvlan mode bridge
sudo ip addr add 10.10.20.239/32 dev leds-shim
sudo ip link set leds-shim up
sudo ip route add 10.10.20.240/32 dev leds-shim
```

Use `type ipvlan mode l2` instead if the network is ipvlan. This does not
survive a reboot on its own; make it permanent with a systemd-networkd
`.netdev` and `.network` pair, or a small `systemd` unit that runs the four
commands at boot.

## Troubleshooting

**Nothing lights up.** Check the status page. If input packets/s is zero the
proxy is not receiving anything, so the problem is upstream: wrong IP, wrong
port, or a firewall. If input is flowing but a target shows errors, the message
is in the `last_error` tooltip on the target row.

**One section is dark.** Run `--check`. Uncovered stretches of the virtual
strip are reported there.

**A section is in the wrong place or backwards.** Send `--pattern sweep` and
watch which device the white pixel is on when it should be elsewhere. `reverse`
handles a strip wired the other way; reordering the `targets` list handles one
that is in the wrong position.

**Colours are wrong on one device only.** Almost always `color_order` being
applied twice. Remove it from the target and set it on the device.

**Art-Net or E1.31 target shows only the first ~1500 pixels.** You have hit
WLED's universe limit. Switch that target to DDP.

**Host names do not resolve in Docker.** `.local` mDNS names need an mDNS
resolver inside the container. Use IP addresses, or DHCP reservations plus real
DNS. The proxy re-resolves names every `resolve_interval_s` seconds, so devices
that move keep working.

**sACN multicast input arrives outside Docker but not inside it.** You are on
bridge networking, which multicast does not cross. Use host networking, or
macvlan, or switch that source to unicast sACN. See
[Networking](#networking).

**The container is `unhealthy` but everything works.** The healthcheck polls
the proxy's own status server, so it fails if you set
`"status": {"enabled": false}` or move the status port without updating
`HEALTH_URL`. Fix whichever applies, or drop the `healthcheck` block.

**The status page is unreachable from the Docker host, but fine from other
machines.** Expected on macvlan and ipvlan. Add the shim interface described
in [Networking](#networking).

## Performance

The hot path uses contiguous slices, precomputed byte gathers and translation
tables, so per-frame work stays in C rather than in Python loops. Rendering and
sending 20,000 pixels to 40 targets, half of them reversed, measures about
0.8 ms of CPU per frame — roughly 3% of one core at 40 fps. A 6,000 pixel,
20 target setup is about 1%. Any small Linux box will do; the network is the
constraint long before the CPU is.

## Running without Docker

```bash
python3 -m wled_proxy --config config.json
```

Python 3.10 or newer, no packages to install. `pip install .` adds `wled-proxy`
and `wled-proxy-testpattern` to your path if you prefer. For a systemd service,
run it as a non-root user and use `Restart=always`; the ports it binds are all
above 1024.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check wled_proxy tests
ruff format --check wled_proxy tests
```

The suite covers the three wire formats against the constants WLED uses, the
pixel mapping and colour conversions, config validation, and end-to-end runs
that feed a real socket and assert on what fake devices receive.

Layout: `protocol.py` is packet encode and decode, `pixels.py` is the virtual
strip and the per-target mapping, `inputs.py` and `outputs.py` are the UDP
ends, `app.py` holds the render loop, and `config.py` is schema and validation.
