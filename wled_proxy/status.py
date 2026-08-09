"""A small HTTP status page, so you can see what the proxy is doing."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 8192

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WLED Proxy</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 2rem 1.5rem; background: #14161a; color: #e7e9ee;
         font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  h1 { font-size: 1.35rem; margin: 0 0 .25rem; }
  .sub { color: #8b93a3; margin-bottom: 1.5rem; font-size: .9rem; }
  .cards { display: flex; flex-wrap: wrap; gap: .75rem; margin-bottom: 1.5rem; }
  .card { background: #1c1f26; border: 1px solid #2a2f3a; border-radius: .6rem;
          padding: .75rem 1rem; min-width: 8.5rem; }
  .card .label { color: #8b93a3; font-size: .75rem; text-transform: uppercase;
                 letter-spacing: .05em; }
  .card .value { font-size: 1.5rem; font-variant-numeric: tabular-nums; }
  .scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
  th, td { text-align: left; padding: .45rem .7rem; border-bottom: 1px solid #262b34;
           white-space: nowrap; }
  th { color: #8b93a3; font-size: .75rem; text-transform: uppercase;
       letter-spacing: .05em; font-weight: 600; }
  tbody tr:hover { background: #1c1f26; }
  .num { text-align: right; }
  .ok { color: #5ed69a; }
  .bad { color: #f2777a; }
  .dim { color: #8b93a3; }
  h2 { font-size: .95rem; margin: 1.75rem 0 .5rem; color: #b9c0cd; }
</style>
</head>
<body>
<h1>WLED Proxy</h1>
<div class="sub" id="sub"></div>
<div class="cards" id="cards"></div>
<h2>Inputs</h2>
<div class="scroll">
<table><thead><tr><th>Protocol</th><th class="num">Packets/s</th><th class="num">Packets</th>
<th class="num">Frames</th><th class="num">Rejected</th><th>Last source</th></tr></thead>
<tbody id="inputs"></tbody></table>
</div>
<h2>Targets</h2>
<div class="scroll">
<table><thead><tr><th>Name</th><th>Address</th><th>Protocol</th><th class="num">Pixels</th>
<th class="num">FPS</th><th class="num">Packets</th><th class="num">Data</th>
<th class="num">Errors</th></tr></thead>
<tbody id="targets"></tbody></table>
</div>
<script>
const fmtBytes = n => n > 1e9 ? (n/1e9).toFixed(1)+" GB"
                  : n > 1e6 ? (n/1e6).toFixed(1)+" MB"
                  : n > 1e3 ? (n/1e3).toFixed(1)+" kB" : n+" B";
const fmtTime = s => {
  const d = Math.floor(s/86400), h = Math.floor(s%86400/3600);
  const m = Math.floor(s%3600/60), sec = Math.floor(s%60);
  return (d ? d+"d " : "") + (d||h ? h+"h " : "") + (d||h||m ? m+"m " : "") + sec+"s";
};
const el = (tag, text, cls) => {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
};
async function refresh() {
  let s;
  try { s = await (await fetch("status.json")).json(); }
  catch (e) { document.getElementById("sub").textContent = "disconnected"; return; }

  document.getElementById("sub").textContent =
    `${s.virtual_strip.led_count} pixel ${s.virtual_strip.format.toUpperCase()} `
    + `virtual strip \\u00b7 up ${fmtTime(s.uptime_s)} \\u00b7 ${s.config}`;

  const cards = [
    ["Input FPS", s.input.frames_per_second.toFixed(1)],
    ["Output FPS", s.output.frames_per_second.toFixed(1)],
    ["Targets", s.output.targets.length],
    ["Packets/frame", s.output.packets_per_frame],
  ];
  const box = document.getElementById("cards");
  box.replaceChildren(...cards.map(([label, value]) => {
    const c = el("div", undefined, "card");
    c.append(el("div", label, "label"), el("div", value, "value"));
    return c;
  }));

  document.getElementById("inputs").replaceChildren(...s.input.sources.map(i => {
    const tr = el("tr");
    tr.append(el("td", i.protocol.toUpperCase()),
              el("td", i.packets_per_second.toFixed(1), "num"),
              el("td", i.packets, "num"), el("td", i.frames, "num"),
              el("td", i.rejected, i.rejected ? "num bad" : "num"),
              el("td", i.last_source || "\\u2014", i.last_source ? "" : "dim"));
    return tr;
  }));

  document.getElementById("targets").replaceChildren(...s.output.targets.map(t => {
    const tr = el("tr");
    const span = `${t.start}\\u2013${t.start + t.count - 1}${t.reverse ? " \\u21c4" : ""}`;
    tr.append(el("td", t.name),
              el("td", `${t.address || t.host}:${t.port}`),
              el("td", `${t.protocol}/${t.format}`),
              el("td", span, "num"),
              el("td", t.frames_per_second.toFixed(1), "num"),
              el("td", t.packets, "num"),
              el("td", fmtBytes(t.bytes), "num"),
              el("td", t.errors, t.errors ? "num bad" : "num ok"));
    if (t.last_error) tr.title = t.last_error;
    return tr;
  }));
}
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""


class StatusServer:
    """Serves the dashboard, ``status.json`` and a health check."""

    def __init__(self, host: str, port: int, provider: Callable[[], dict]):
        self.host = host
        self.port = port
        self.provider = provider
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request or len(request) > MAX_REQUEST_BYTES:
                return
            parts = request.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1].split("?", 1)[0]

            while True:  # discard headers
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            if method not in ("GET", "HEAD"):
                await self._respond(
                    writer, 405, "text/plain", b"method not allowed", method
                )
            elif path in ("/", "/index.html"):
                await self._respond(
                    writer,
                    200,
                    "text/html; charset=utf-8",
                    PAGE.encode("utf-8"),
                    method,
                )
            elif path in ("/status.json", "/status", "/api/status"):
                body = json.dumps(self.provider(), indent=1).encode("utf-8")
                await self._respond(writer, 200, "application/json", body, method)
            elif path in ("/healthz", "/health"):
                await self._respond(writer, 200, "text/plain", b"ok\n", method)
            else:
                await self._respond(writer, 404, "text/plain", b"not found\n", method)
        except (asyncio.TimeoutError, TimeoutError, ConnectionError):
            pass
        except Exception:
            log.debug("status request failed", exc_info=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        code: int,
        content_type: str,
        body: bytes,
        method: str,
    ) -> None:
        reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed"}[code]
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head if method == "HEAD" else head + body)
        await writer.drain()
