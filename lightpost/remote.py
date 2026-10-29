"""Optional public address for tools that cannot reach localhost. Off unless you choose it.

LightPost never opens anything by default. You can choose one of two ways to be reachable:

- ngrok: your own ngrok account or ngrok configuration. LightPost runs the ngrok program you
  installed; an authtoken and a static domain are optional, because ngrok may already be set up.
- custom: an https address you already provide yourself (Tailscale Funnel, a Cloudflare named
  tunnel, your own reverse proxy, ...). LightPost starts nothing and only shows the addresses.

Whatever carries the traffic can read it. Only the key-protected /api/v1 and /mcp endpoints are
reachable from a public address; every other request needs ADMIN_PASSWORD or is refused.
"""

import asyncio
import collections
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .app import KEYS, read_json

MODES = ("off", "ngrok", "custom")

NGROK_HELP = (
    "ngrok is not installed on this computer. Install it from https://ngrok.com/download, "
    "then press Save again."
)
NGROK_ADDRESS = re.compile(r"url=(https://[A-Za-z0-9.-]+)")
DOMAIN = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
TOKEN = re.compile(r"[A-Za-z0-9_.-]{16,256}")
PROBLEM = re.compile(r"\bERR\b|ERR_NGROK|lvl=eror|\berror\b", re.I)


def clean_domain(value):
    """Accept 'my-name.ngrok-free.app' or a pasted 'https://my-name.ngrok-free.app/'."""
    domain = re.sub(r"^https?://", "", str(value or "").strip().lower()).strip("/")
    if len(domain) > 253 or not DOMAIN.fullmatch(domain):
        raise ValueError("Enter your ngrok domain, like my-name.ngrok-free.app.")
    return domain


def clean_public_url(value):
    """Accept 'https://lightpost.example.com' (a trailing slash is dropped)."""
    problem = "Enter the full https address, like https://lightpost.example.com (no path)."
    try:
        parts = urlsplit(str(value or "").strip())
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        raise ValueError(problem) from None
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or not (DOMAIN.fullmatch(host) or ":" in host)  # A name, or an IPv6 literal.
    ):
        raise ValueError(problem)
    return f"https://{parts.netloc.lower()}"


def clean_config_path(value):
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_file():
        raise ValueError("That ngrok configuration file was not found on this computer.")
    return str(path)


def instructions(address):
    """What is printed on startup. The key is never printed here, so a screenshot of the terminal
    cannot leak it. Settings > Automation shows it, or run: python -m lightpost api-key."""
    return "\n".join(
        [
            "",
            f"Public address: {address}",
            "",
            "  MCP server (send the key as an 'Authorization: Bearer <key>' header):",
            f"    {address}/mcp",
            "  MCP connector address for tools that cannot send headers:",
            f"    {address}/mcp/<your key>",
            "  REST API:",
            f"    {address}/api/v1   (OpenAPI: {address}/api/v1/openapi.json)",
            "",
            "  Your key is not shown here. Copy it, or the full connector address, from",
            "  Settings > Automation, or run: python -m lightpost api-key",
            "",
        ]
    )


class Remote:
    def __init__(self, app, say=print):
        self.app, self.say = app, say
        self.state = {"status": "off", "url": "", "message": ""}
        self.task = None
        self.lock = asyncio.Lock()
        self.retry_delay = 5  # Doubles up to a minute while the tunnel keeps failing.

    @property
    def config(self):
        return self.app[KEYS["config"]]

    def set(self, status, url="", message=""):
        self.state = {"status": status, "url": url, "message": message}

    async def saved(self):
        record = await self.app[KEYS["store"]].get("settings", "remote") or {}
        mode = record.get("mode", "off")
        url = record.get("url", "")
        if self.config.public_url:  # PUBLIC_URL in .env wins over the saved choice.
            mode, url = "custom", self.config.public_url
        return {
            "mode": mode,
            "domain": record.get("domain", ""),
            "token": record.get("token", ""),
            "config": record.get("config", ""),
            "url": url,
        }

    async def public(self):
        saved = await self.saved()
        return {
            "mode": saved["mode"],
            "domain": saved["domain"],
            "tokenSaved": bool(saved["token"]),
            "configPath": saved["config"],
            "publicUrl": saved["url"],
            "locked": bool(self.config.public_url),
            **self.state,
        }

    async def update(self, data):
        if self.config.public_url:
            raise ValueError("The public address is set by PUBLIC_URL in your .env file.")
        mode = data.get("mode")
        if mode not in MODES:
            raise ValueError("Choose off, ngrok, or your own address.")
        store, vault = self.app[KEYS["store"]], self.app[KEYS["vault"]]
        record = await store.get("settings", "remote") or {}
        domain, token = record.get("domain", ""), record.get("token", "")
        config, url = record.get("config", ""), record.get("url", "")
        if mode == "ngrok":
            # Everything is optional: ngrok may already be set up on this computer.
            if "domain" in data:
                domain = clean_domain(data["domain"]) if str(data["domain"] or "").strip() else ""
            if "configPath" in data:
                config = clean_config_path(data["configPath"])
            fresh = str(data.get("token") or "").strip()
            if fresh:
                if not TOKEN.fullmatch(fresh):
                    raise ValueError("That does not look like an ngrok authtoken. Copy it again.")
                token = vault.encrypt({"token": fresh})
            if data.get("clearToken"):
                token = ""
        elif mode == "custom":
            url = clean_public_url(data.get("publicUrl", url))
        await store.put(
            "settings",
            {
                "id": "remote",
                "mode": mode,
                "domain": domain,
                "token": token,
                "config": config,
                "url": url,
            },
        )
        await self.apply()
        return await self.public()

    async def apply(self):
        """Start, restart or stop the tunnel to match the saved settings."""
        async with self.lock:
            await self.stop()
            saved = await self.saved()
            if saved["mode"] == "off":
                self.set("off")
                return
            if saved["mode"] == "custom":
                # Nothing to start: the person provides the public https address themselves.
                self.announce(saved["url"])
                return
            token = ""
            if saved["mode"] == "ngrok" and saved["token"]:
                try:
                    token = self.app[KEYS["vault"]].decrypt(saved["token"])["token"]
                except (ValueError, KeyError):
                    self.set("error", message="Cannot read the saved ngrok token. Paste it again.")
                    return
            self.set("starting")
            self.task = asyncio.create_task(
                self.supervise(saved["mode"], saved["domain"], token, saved["config"])
            )

    async def stop(self):
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def supervise(self, mode, domain, token, config=""):
        delay = self.retry_delay
        while True:
            started = time.monotonic()
            try:
                program = await self.ngrok()
                await self.run_ngrok(program, domain, token, config)
            except asyncio.CancelledError:
                raise
            except ValueError as error:
                self.set("error", message=str(error))
                if getattr(error, "permanent", False):
                    return
            except Exception:
                self.set("error", message="The tunnel stopped unexpectedly.")
            # The program ended on its own. Try again, waiting longer if it keeps failing.
            if time.monotonic() - started > 60:
                delay = self.retry_delay
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
            self.set("starting", message=self.state["message"])

    # -- finding the program ------------------------------------------------------------------

    async def ngrok(self):
        found = shutil.which("ngrok")
        if not found:
            raise Permanent(NGROK_HELP)
        return found

    # -- running a tunnel ----------------------------------------------------------------------

    async def watch(self, process, token="", on_line=None):
        """Read the program's output until it ends. Returns the lines that looked like problems."""
        lines = collections.deque(maxlen=8)
        while True:
            try:
                raw = await process.stdout.readline()
            except ValueError:  # An unusually long line; skip it.
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if token:
                line = line.replace(token, "***")
            if line:
                lines.append(line)
            if on_line:
                on_line(line)
        await process.wait()
        # From the first line that looks like a problem onward: ngrok prints "ERROR:" on one
        # line and the actual reason on the lines after it.
        first = next((i for i, text in enumerate(lines) if PROBLEM.search(text)), None)
        return [] if first is None else list(lines)[first:]

    async def launch(self, argv, env=None):
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            limit=1 << 20,
        )

    async def finish(self, process):
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    def announce(self, address):
        self.set("running", url=address)
        self.say(instructions(address))

    async def run_ngrok(self, program, domain, token, config=""):
        env = dict(os.environ)  # Keeps an NGROK_AUTHTOKEN or ngrok.yml the person already has.
        if token:
            env["NGROK_AUTHTOKEN"] = token  # Not on the command line, so not in `ps`.
        extra = ["--config", config] if config else []
        problems = []
        # Newer ngrok versions take --url; older ones take --domain. Without a domain, ngrok
        # picks the address (or uses the one in the person's own configuration) and LightPost
        # reads it from ngrok's log.
        flags = [f"--url=https://{domain}", f"--domain={domain}"] if domain else [None]
        for flag in flags:
            process = await self.launch(
                [
                    program,
                    "http",
                    f"127.0.0.1:{self.config.port}",
                    *([flag] if flag else []),
                    *extra,
                    "--log=stdout",
                    "--log-format=logfmt",
                ],
                env=env,
            )
            known = f"https://{domain}" if domain else ""

            def on_line(line, known=known):
                if self.state["status"] == "running" or "started tunnel" not in line:
                    return
                found = NGROK_ADDRESS.search(line)
                if known or found:
                    self.announce(known or found[1])

            async def assume_running(process=process, known=known):
                await asyncio.sleep(8)
                if known and process.returncode is None and self.state["status"] != "running":
                    self.announce(known)

            self.set("starting", url=known)
            started = time.monotonic()
            timer = asyncio.create_task(assume_running())
            try:
                problems = await self.watch(process, token=token, on_line=on_line)
            finally:
                timer.cancel()
                await self.finish(process)
            unknown_flag = any(re.search(r"unknown (flag|shorthand)", p, re.I) for p in problems)
            if not (unknown_flag and time.monotonic() - started < 10):
                break
        raise ValueError(failure("ngrok stopped", process, problems))


class Permanent(ValueError):
    """A problem retrying will not fix; the tunnel stays stopped until settings change."""

    permanent = True


def failure(prefix, process, problems):
    detail = " ".join(problems)[-400:] if problems else f"exit code {process.returncode}"
    if re.search(r"ERR_NGROK_(108|334)|simultaneous|already online", detail, re.I):
        detail += (
            " ngrok allows one session at a time on the free plan, and one use of a domain at "
            "a time. Close any other ngrok (including one running in another window) and "
            "press Save again."
        )
    return f"{prefix}: {detail}"


# -- Settings page endpoints (browser session, like the rest of /api) ---------------------------


async def remote_info(request):
    return web.json_response(await request.app[KEYS["remote"]].public())


async def remote_update(request):
    return web.json_response(await request.app[KEYS["remote"]].update(await read_json(request)))


def routes():
    return [web.get("/api/remote", remote_info), web.put("/api/remote", remote_update)]
