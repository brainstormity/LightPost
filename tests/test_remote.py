import asyncio
import json
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from lightpost.app import KEYS, create_app
from lightpost.config import Config
from lightpost.remote import (
    clean_domain,
    clean_public_url,
    instructions,
)
from lightpost.store import Store

warnings.filterwarnings("ignore", category=web.NotAppKeyWarning)
KEY = "a1" * 32  # Test-only encryption key.
API_KEY = "test-api-key-" + "k" * 24  # Test-only API key.
NGROK_TOKEN = "2abcDEF_fakeTokenForTests1234567890"  # Not a real credential.


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class RemoteCase(unittest.IsolatedAsyncioTestCase):
    password = ""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.bin = self.root / "fakebin"
        self.bin.mkdir()
        # Only the stand-in programs are on PATH, so a real ngrok is never used.
        self.env = patch.dict(os.environ, {"PATH": str(self.bin)})
        self.env.start()
        self.said = []
        self.config = Config(
            database=str(self.root / "t.sqlite"),
            key=KEY,
            scheduler=False,
            api_key=API_KEY,
            password=self.password,
            say=self.said.append,
        )
        self.client = None

    async def asyncTearDown(self):
        if self.client:
            await self.client.close()
        self.env.stop()
        self.directory.cleanup()

    async def start(self):
        self.client = TestClient(TestServer(create_app(self.config)))
        await self.client.start_server()
        self.remote = self.client.server.app[KEYS["remote"]]
        self.remote.retry_delay = 0.05
        return self.remote

    async def seed(self, **record):
        store = await Store(self.config.database).open()
        await store.put("settings", {"id": "remote", **record})
        await store.close()

    def program(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return path

    def fake_ngrok(self, body=None):
        return self.program(
            "ngrok",
            body
            or f"""echo $$ >> "{self.root}/ng.pids"
echo "$@" > "{self.root}/ng.args"
echo "$NGROK_AUTHTOKEN" > "{self.root}/ng.token"
echo 't=1 lvl=info msg="started tunnel" obj=tunnels url=https://my-name.ngrok-free.app'
exec /bin/sleep 30
""",
        )

    def pids(self, name):
        path = self.root / name
        return [int(p) for p in path.read_text().split()] if path.exists() else []

    async def until(self, check, what="the expected state", timeout=6):
        for _ in range(int(timeout / 0.05)):
            if check():
                return
            await asyncio.sleep(0.05)
        self.fail(f"Timed out waiting for {what}; state was {self.remote.state}")

    async def put(self, **body):
        return await self.client.put("/api/remote", json=body)


class SettingsTests(RemoteCase):
    async def test_off_by_default_and_nothing_is_started(self):
        await self.start()
        info = await (await self.client.get("/api/remote")).json()
        self.assertEqual(
            info,
            {
                "mode": "off",
                "domain": "",
                "tokenSaved": False,
                "configPath": "",
                "publicUrl": "",
                "locked": False,
                "status": "off",
                "url": "",
                "message": "",
            },
        )
        self.assertEqual(self.said, [])

    async def test_invalid_choices_are_rejected(self):
        await self.start()
        for body in (
            {},
            {"mode": "everywhere"},
            {"mode": "custom"},
            {"mode": "custom", "publicUrl": "http://plain.example.com"},
            {"mode": "custom", "publicUrl": "https://example.com/some/path"},
            {"mode": "custom", "publicUrl": "https://user:pw@example.com"},
            {"mode": "custom", "publicUrl": "https://example.com:notaport"},
            {"mode": "custom", "publicUrl": "https://localhost"},
            {"mode": "ngrok", "domain": "not a domain", "token": NGROK_TOKEN},
            {"mode": "ngrok", "configPath": "/no/such/ngrok.yml"},
            {"mode": "ngrok", "domain": "my-name.ngrok-free.app", "token": "short"},
            {
                "mode": "ngrok",
                "domain": "my-name.ngrok-free.app",
                "token": "bad token with spaces!!",
            },
        ):
            with self.subTest(body=body):
                self.assertEqual((await self.put(**body)).status, 400)
        self.assertEqual((await self.client.get("/api/remote")).status, 200)

    def test_public_urls_accept_pasted_https_addresses_only(self):
        for given, expected in (
            ("https://LightPost.Example.com/", "https://lightpost.example.com"),
            (" https://host.tailnet.ts.net ", "https://host.tailnet.ts.net"),
            ("https://example.com:8443", "https://example.com:8443"),
        ):
            self.assertEqual(clean_public_url(given), expected)

    def test_domains_accept_pasted_addresses_and_reject_junk(self):
        self.assertEqual(clean_domain("https://My-Name.ngrok-free.app/"), "my-name.ngrok-free.app")
        self.assertEqual(clean_domain(" my-name.ngrok.dev "), "my-name.ngrok.dev")
        for bad in (
            "",
            "localhost",
            "my name.ngrok.app",
            "a..b.com",
            "-x.com",
            "https://",
            "x" * 300 + ".com",
            None,
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clean_domain(bad)

    def test_instructions_cover_every_way_to_connect(self):
        text = instructions("https://a.example.dev", "lp_secret")
        for expected in (
            "https://a.example.dev/mcp",
            "https://a.example.dev/mcp/lp_secret",
            "/api/v1",
            "Authorization: Bearer",
        ):
            self.assertIn(expected, text)


class SupervisorTests(RemoteCase):
    async def test_a_tunnel_that_dies_is_restarted(self):
        self.fake_ngrok(body=f'echo $$ >> "{self.root}/ng.pids"\necho "ERROR: boom"\nexit 1\n')
        await self.start()
        await self.put(mode="ngrok")  # After start, so the short test delay applies.
        await self.until(lambda: len(self.pids("ng.pids")) >= 3, "repeated restarts")
        self.assertIn(self.remote.state["status"], ("error", "starting", "running"))

    async def test_a_saved_choice_starts_by_itself_on_the_next_launch(self):
        self.fake_ngrok()
        await self.seed(mode="ngrok", domain="", token="", config="")
        await self.start()
        await self.until(lambda: self.remote.state["status"] == "running", "an automatic start")
        self.assertEqual(self.remote.state["url"], "https://my-name.ngrok-free.app")

    async def test_closing_the_app_stops_the_tunnel(self):
        self.fake_ngrok()
        await self.seed(mode="ngrok", domain="", token="", config="")
        await self.start()
        await self.until(lambda: self.remote.state["status"] == "running")
        pid = self.pids("ng.pids")[-1]
        await self.client.close()
        self.client = None
        self.assertFalse(alive(pid))

    async def test_turning_it_off_stops_the_tunnel(self):
        self.fake_ngrok()
        await self.start()
        await self.put(mode="ngrok")
        await self.until(lambda: self.remote.state["status"] == "running")
        pid = self.pids("ng.pids")[-1]
        info = await (await self.put(mode="off")).json()
        self.assertEqual((info["mode"], info["status"], info["url"]), ("off", "off", ""))
        self.assertFalse(alive(pid))


class OwnAddressTests(RemoteCase):
    """Any tunnel or reverse proxy you run yourself: LightPost only shows the addresses."""

    async def test_your_own_https_address_starts_nothing_and_is_announced(self):
        self.fake_ngrok()
        await self.start()
        response = await self.put(mode="custom", publicUrl="https://Lp.Example.com/")
        self.assertEqual(response.status, 200)
        info = await response.json()
        self.assertEqual(
            (info["status"], info["url"], info["publicUrl"]),
            ("running", "https://lp.example.com", "https://lp.example.com"),
        )
        self.assertIn(f"https://lp.example.com/mcp/{API_KEY}", self.said[0])
        self.assertEqual(self.pids("ng.pids"), [])
        self.assertIsNone(self.remote.task)

    async def test_it_is_remembered_and_can_be_turned_off(self):
        await self.seed(mode="custom", url="https://lp.example.com")
        await self.start()
        self.assertEqual(
            (self.remote.state["status"], self.remote.state["url"]),
            ("running", "https://lp.example.com"),
        )
        info = await (await self.put(mode="off")).json()
        self.assertEqual((info["status"], info["url"]), ("off", ""))

    async def test_public_url_in_the_environment_wins_and_locks_the_setting(self):
        self.config.public_url = "https://from-env.example.com"
        await self.start()
        info = await (await self.client.get("/api/remote")).json()
        self.assertEqual(
            (info["mode"], info["url"], info["locked"]),
            ("custom", "https://from-env.example.com", True),
        )
        self.assertEqual((await self.put(mode="off")).status, 400)

    def test_public_url_from_env_is_checked_when_loading_config(self):
        from lightpost.config import Config as Loaded

        with (
            patch.dict(os.environ, {"PUBLIC_URL": "http://insecure.example.com"}),
            patch("lightpost.config.load_env"),
        ):
            with self.assertRaises(ValueError):
                Loaded.from_env()
        with (
            patch.dict(os.environ, {"PUBLIC_URL": "https://ok.example.com/"}),
            patch("lightpost.config.load_env"),
        ):
            self.assertEqual(Loaded.from_env().public_url, "https://ok.example.com")


class NgrokTests(RemoteCase):
    async def save_ngrok(self, **extra):
        body = {
            "mode": "ngrok",
            "domain": "https://My-Name.ngrok-free.app/",
            "token": NGROK_TOKEN,
            **extra,
        }
        return await self.put(**body)

    async def test_stable_address_runs_with_the_token_kept_out_of_view(self):
        self.fake_ngrok()
        await self.start()
        response = await self.save_ngrok()
        self.assertEqual(response.status, 200)
        await self.until(lambda: self.remote.state["status"] == "running", "ngrok to run")
        self.assertEqual(self.remote.state["url"], "https://my-name.ngrok-free.app")
        arguments = (self.root / "ng.args").read_text()
        self.assertIn("--url=https://my-name.ngrok-free.app", arguments)
        self.assertIn(f"127.0.0.1:{self.config.port}", arguments)
        self.assertNotIn(NGROK_TOKEN, arguments)  # Not visible in the process list.
        self.assertEqual((self.root / "ng.token").read_text().strip(), NGROK_TOKEN)
        self.assertIn(f"https://my-name.ngrok-free.app/mcp/{API_KEY}", self.said[0])

    async def test_the_token_is_stored_encrypted_and_never_sent_back(self):
        self.fake_ngrok()
        await self.start()
        await self.save_ngrok()
        store = self.client.server.app[KEYS["store"]]
        record = await store.get("settings", "remote")
        self.assertNotIn(NGROK_TOKEN, json.dumps(record))
        self.assertEqual(record["domain"], "my-name.ngrok-free.app")
        text = await (await self.client.get("/api/remote")).text()
        self.assertNotIn(NGROK_TOKEN, text)
        self.assertNotIn(record["token"], text)
        info = json.loads(text)
        self.assertEqual((info["tokenSaved"], info["domain"]), (True, "my-name.ngrok-free.app"))
        # Saving again without a token keeps the saved one.
        response = await self.put(mode="ngrok", domain="my-name.ngrok-free.app")
        self.assertEqual(response.status, 200)
        await self.until(lambda: len(self.pids("ng.pids")) == 2)
        self.assertEqual((self.root / "ng.token").read_text().strip(), NGROK_TOKEN)

    async def test_your_own_ngrok_setup_needs_no_token_or_domain(self):
        self.fake_ngrok()
        await self.start()
        with patch.dict(os.environ, {"NGROK_AUTHTOKEN": "from-the-environment-123456"}):
            response = await self.put(mode="ngrok")
            self.assertEqual(response.status, 200)
            await self.until(lambda: self.remote.state["status"] == "running", "ngrok to run")
        self.assertEqual(
            self.remote.state["url"], "https://my-name.ngrok-free.app"
        )  # Read from ngrok's log.
        arguments = (self.root / "ng.args").read_text()
        self.assertNotIn("--url", arguments)
        self.assertNotIn("--domain", arguments)
        self.assertEqual(
            (self.root / "ng.token").read_text().strip(), "from-the-environment-123456"
        )
        info = await (await self.client.get("/api/remote")).json()
        self.assertEqual((info["tokenSaved"], info["domain"]), (False, ""))

    async def test_a_chosen_ngrok_config_file_is_passed_along(self):
        self.fake_ngrok()
        config = self.root / "my-ngrok.yml"
        config.write_text("version: 3\n")
        await self.start()
        response = await self.put(mode="ngrok", configPath=str(config))
        self.assertEqual(response.status, 200)
        await self.until(lambda: self.remote.state["status"] == "running")
        self.assertIn(f"--config {config}", (self.root / "ng.args").read_text())
        self.assertEqual((await response.json())["configPath"], str(config))

    async def test_a_saved_token_can_be_removed(self):
        self.fake_ngrok()
        await self.start()
        await self.save_ngrok()
        info = await (await self.put(mode="ngrok", clearToken=True)).json()
        self.assertFalse(info["tokenSaved"])
        await self.until(lambda: len(self.pids("ng.pids")) == 2)
        self.assertEqual((self.root / "ng.token").read_text().strip(), "")

    async def test_older_ngrok_versions_get_the_domain_flag(self):
        self.fake_ngrok(
            body=f"""case "$*" in *--url=*) echo 'ERROR: unknown flag: --url' ; exit 2 ;; esac
echo "$@" > "{self.root}/ng.args"
echo 't=1 lvl=info msg="started tunnel" url=https://my-name.ngrok-free.app'
exec /bin/sleep 30
"""
        )
        await self.start()
        await self.save_ngrok()
        await self.until(
            lambda: self.remote.state["status"] == "running", "the fallback flag to work"
        )
        self.assertIn("--domain=my-name.ngrok-free.app", (self.root / "ng.args").read_text())

    async def test_a_quiet_but_running_ngrok_is_treated_as_up(self):
        self.fake_ngrok(body="exec /bin/sleep 30\n")
        await self.start()
        with patch("lightpost.remote.asyncio.sleep", new=_quick_sleep()):
            await self.save_ngrok()
            await self.until(lambda: self.remote.state["status"] == "running")

    async def test_errors_are_shown_without_leaking_the_token(self):
        self.fake_ngrok(
            body=f"echo 'ERR_NGROK_105 authentication failed for {NGROK_TOKEN}'\nexit 1\n"
        )
        await self.start()
        await self.save_ngrok()
        await self.until(lambda: self.remote.state["status"] == "error")
        message = self.remote.state["message"]
        self.assertIn("ERR_NGROK_105", message)
        self.assertNotIn(NGROK_TOKEN, message)
        self.assertNotIn(
            NGROK_TOKEN, json.dumps(await (await self.client.get("/api/remote")).json())
        )

    async def test_a_multi_line_ngrok_error_is_shown_in_full(self):
        self.fake_ngrok(
            body="echo 'ERROR:'\necho 'ERROR:  authentication failed: The domain is reserved by another account.'\necho 'ERROR:  ERR_NGROK_320'\nexit 1\n"
        )
        await self.start()
        await self.save_ngrok()
        await self.until(lambda: self.remote.state["status"] == "error")
        self.assertIn("reserved by another account", self.remote.state["message"])
        self.assertIn("ERR_NGROK_320", self.remote.state["message"])

    async def test_the_one_session_limit_is_explained(self):
        self.fake_ngrok(
            body="echo 'ERROR:  Your account is limited to 1 simultaneous ngrok agent sessions.'\necho 'ERROR:  ERR_NGROK_108'\nexit 1\n"
        )
        await self.start()
        await self.save_ngrok()
        await self.until(lambda: self.remote.state["status"] == "error")
        self.assertIn("one session at a time", self.remote.state["message"])
        self.assertIn("press Save again", self.remote.state["message"])

    async def test_missing_ngrok_explains_how_to_install_it(self):
        await self.start()
        await self.save_ngrok()
        await self.until(lambda: self.remote.state["status"] == "error")
        self.assertIn("https://ngrok.com/download", self.remote.state["message"])
        await self.until(lambda: self.remote.task.done())
        # The choice is saved, so installing ngrok and pressing Save again is enough.
        self.fake_ngrok()
        response = await self.put(mode="ngrok", domain="my-name.ngrok-free.app")
        self.assertEqual(response.status, 200)
        await self.until(lambda: self.remote.state["status"] == "running")

    async def test_an_unreadable_saved_token_asks_for_it_again(self):
        await self.seed(mode="ngrok", domain="my-name.ngrok-free.app", token="not-encrypted")
        await self.start()
        self.assertEqual(self.remote.state["status"], "error")
        self.assertIn("Paste it again", self.remote.state["message"])


def _quick_sleep():
    real = asyncio.sleep

    async def sleep(delay, *args):
        return await real(min(delay, 0.05), *args)

    return sleep


class AccessTests(RemoteCase):
    password = "p" * 20

    async def test_only_a_signed_in_owner_can_read_or_change_remote_access(self):
        await self.start()
        self.assertEqual((await self.client.get("/api/remote")).status, 401)
        self.assertEqual((await self.put(mode="ngrok")).status, 401)
        bearer = {"Authorization": "Bearer " + API_KEY}
        self.assertEqual((await self.client.get("/api/remote", headers=bearer)).status, 401)
        await self.client.post("/api/login", json={"password": self.password})
        self.assertEqual((await self.client.get("/api/remote")).status, 200)


class LocalAccessTests(RemoteCase):
    async def test_public_hostnames_cannot_change_remote_access_without_a_password(self):
        await self.start()
        headers = {"Host": "lightpost.example.com"}
        self.assertEqual((await self.client.get("/api/remote", headers=headers)).status, 403)
        response = await self.client.put("/api/remote", json={"mode": "ngrok"}, headers=headers)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.remote.state["status"], "off")


class ExternalRequestTests(RemoteCase):
    """Anything that arrives through a public hostname needs the API key, or is refused."""

    HOST = {"Host": "lightpost.example.com"}

    async def test_public_hostnames_only_reach_key_protected_endpoints(self):
        await self.start()

        async def get(path, **kw):
            headers = {**self.HOST, **kw.pop("headers", {})}
            return await self.client.get(path, headers=headers, **kw)

        for path in ("/api/v1/accounts", "/api/v1/posts", "/api/v1/openapi.json", "/api/v1/media"):
            with self.subTest(path=path):
                self.assertEqual((await get(path)).status, 401)
        for headers in (
            {"Authorization": "Bearer wrong"},
            {"Authorization": "Bearer "},
            {"Authorization": "Bearer " + API_KEY[:-1]},
            {"X-API-Key": "x"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual((await get("/api/v1/accounts", headers=headers)).status, 401)
        for path in ("/mcp/wrong-key", "/mcp/", "/mcp"):
            with self.subTest(path=path):
                response = await self.client.post(path, json={}, headers=self.HOST)
                self.assertEqual(response.status, 401)
        for path in (
            "/",
            "/app.js",
            "/api/posts",
            "/api/accounts",
            "/api/automation",
            "/api/remote",
            "/api/export",
        ):
            with self.subTest(path=path):
                self.assertEqual((await get(path)).status, 403)
        key = {"Authorization": "Bearer " + API_KEY}
        self.assertEqual((await get("/api/v1/accounts", headers=key)).status, 200)
        self.assertEqual((await get("/api/v1/openapi.json", headers=key)).status, 200)

    async def test_an_empty_key_never_authenticates(self):
        await self.start()
        self.client.server.app[KEYS["config"]].api_key = ""
        for headers in ({"Authorization": "Bearer "}, {"X-API-Key": ""}, {}):
            response = await self.client.get("/api/v1/accounts", headers={**self.HOST, **headers})
            self.assertEqual(response.status, 401)
        self.assertEqual((await self.client.post("/mcp/", json={}, headers=self.HOST)).status, 401)

    async def test_the_key_does_not_open_the_dashboard_api(self):
        await self.start()
        headers = {**self.HOST, "Authorization": "Bearer " + API_KEY}
        self.assertEqual((await self.client.get("/api/posts", headers=headers)).status, 403)
        self.assertEqual(
            (await self.client.put("/api/remote", json={"mode": "off"}, headers=headers)).status,
            403,
        )

    async def test_a_switched_off_api_stays_closed_from_outside(self):
        self.config.api_enabled = False
        await self.start()
        headers = {**self.HOST, "Authorization": "Bearer " + API_KEY}
        self.assertEqual((await self.client.get("/api/v1/accounts", headers=headers)).status, 403)


class LocalOnlyDefaultTests(RemoteCase):
    async def test_nothing_is_opened_or_asked_by_default(self):
        self.fake_ngrok()
        await self.start()
        await asyncio.sleep(0.3)
        self.assertEqual(self.remote.state["status"], "off")
        self.assertEqual(self.pids("ng.pids"), [])
        self.assertEqual(self.said, [])
        store = self.client.server.app[KEYS["store"]]
        self.assertIsNone(await store.get("settings", "remote"))


if __name__ == "__main__":
    unittest.main()
