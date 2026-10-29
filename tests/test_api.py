import asyncio
import base64
import os
import stat
import tempfile
import unittest
import warnings
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from lightpost.api import POST_FIELDS, TOOLS
from lightpost.app import KEYS, create_app
from lightpost.config import Config
from lightpost.providers import ProviderError, now

warnings.filterwarnings("ignore", category=web.NotAppKeyWarning)
KEY = "a1" * 32  # Test-only encryption key.
API_KEY = "test-api-key-" + "k" * 24  # Test-only API key.
AUTH = {"Authorization": "Bearer " + API_KEY}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jM1sAAAAASUVORK5CYII="
)
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
FUTURE = "2030-01-15T09:30:00"


class AutomationCase(unittest.IsolatedAsyncioTestCase):
    api_key = API_KEY
    api_enabled = True
    password = ""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.send = AsyncMock()
        self.config = Config(
            database=str(Path(self.directory.name) / "test.sqlite"),
            key=KEY,
            scheduler=False,
            api_key=self.api_key,
            api_enabled=self.api_enabled,
            password=self.password,
        )
        self.client = TestClient(TestServer(create_app(self.config, self.send)))
        await self.client.start_server()
        self.app = self.client.server.app
        self.store = self.app[KEYS["store"]]
        self.jobs = self.app[KEYS["jobs"]]
        for platform, handle in (("bluesky", "@me.bsky.social"), ("mastodon", "@me@masto.example")):
            await self.store.put(
                "accounts",
                {
                    "id": "id-" + platform,
                    "platform": platform,
                    "name": platform.title(),
                    "handle": handle,
                    "credentials": "SECRET-CIPHERTEXT",
                    "demo": False,
                },
            )
        self.app[KEYS["providers"]].publish = AsyncMock(return_value={"status": "published"})

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def api(self, method, path, **kwargs):
        kwargs.setdefault("headers", AUTH)
        return await self.client.request(method, "/api/v1" + path, **kwargs)

    async def create(self, **fields):
        return await self.api("POST", "/posts", json={"accounts": ["bluesky"], **fields})

    async def rpc(self, method, params=None, id=1, headers=AUTH):
        body = {"jsonrpc": "2.0", "method": method}
        if id is not None:
            body["id"] = id
        if params is not None:
            body["params"] = params
        return await self.client.post("/mcp", json=body, headers=headers)

    async def call_tool(self, name, arguments=None):
        response = await self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        self.assertEqual(response.status, 200)
        return (await response.json())["result"]


class AuthTests(AutomationCase):
    async def test_missing_and_wrong_keys_are_rejected(self):
        for path in ("/api/v1/accounts", "/api/v1/posts", "/api/v1/openapi.json"):
            self.assertEqual((await self.client.get(path)).status, 401)
            bad = {"Authorization": "Bearer wrong"}
            self.assertEqual((await self.client.get(path, headers=bad)).status, 401)
        self.assertEqual((await self.client.post("/mcp", json={})).status, 401)
        self.assertEqual((await self.client.post("/mcp/", json={})).status, 401)

    async def test_bearer_and_x_api_key_headers_work(self):
        self.assertEqual((await self.api("GET", "/accounts")).status, 200)
        header = {"X-API-Key": API_KEY}
        self.assertEqual((await self.api("GET", "/accounts", headers=header)).status, 200)

    async def test_key_works_for_any_hostname_but_the_ui_api_still_needs_loopback(self):
        headers = {**AUTH, "Host": "lightpost.example.com"}
        self.assertEqual((await self.api("GET", "/accounts", headers=headers)).status, 200)
        self.assertEqual((await self.client.get("/api/state", headers=headers)).status, 403)

    async def test_credentials_never_leave_the_server(self):
        body = await (await self.api("GET", "/accounts")).text()
        self.assertNotIn("SECRET-CIPHERTEXT", body)
        self.assertNotIn("credentials", body)

    async def test_unknown_v1_paths_are_authenticated_too(self):
        self.assertEqual((await self.client.get("/api/v1/nothing")).status, 401)


class DisabledTests(AutomationCase):
    api_enabled = False

    async def test_api_can_be_switched_off_in_the_environment(self):
        response = await self.client.get("/api/v1/accounts", headers=AUTH)
        self.assertEqual(response.status, 403)
        self.assertIn("turned off", (await response.json())["error"])
        self.assertEqual((await self.client.post("/mcp", json={}, headers=AUTH)).status, 403)
        self.assertEqual((await self.client.post("/mcp/" + API_KEY, json={})).status, 403)
        response = await self.client.put("/api/automation", json={"enabled": True})
        self.assertEqual(response.status, 400)
        self.assertIn("API_ENABLED", (await response.json())["error"])


class PasswordWorkspaceTests(AutomationCase):
    password = "p" * 20

    async def test_browser_session_does_not_grant_api_access(self):
        self.assertEqual(
            (await self.client.post("/api/login", json={"password": "p" * 20})).status, 200
        )
        self.assertEqual((await self.client.get("/api/state")).status, 200)
        self.assertEqual((await self.client.get("/api/v1/accounts")).status, 401)
        self.assertEqual((await self.api("GET", "/accounts")).status, 200)


class ConfigTests(unittest.TestCase):
    def test_short_api_keys_are_refused(self):
        with (
            patch.dict(os.environ, {"API_KEY": "short"}),
            self.assertRaisesRegex(ValueError, "at least 24"),
        ):
            Config.from_env()

    def test_api_key_is_read_from_the_environment(self):
        with patch.dict(os.environ, {"API_KEY": "z" * 30}):
            self.assertEqual(Config.from_env().api_key, "z" * 30)

    def test_api_key_command_prints_a_usable_key(self):
        from lightpost.__main__ import main

        with (
            patch("sys.argv", ["lightpost", "api-key"]),
            patch("sys.stdout", new=StringIO()) as out,
        ):
            main()
        self.assertGreaterEqual(len(out.getvalue().strip()), 32)


class PostTests(AutomationCase):
    async def test_defaults_to_a_safe_draft(self):
        response = await self.create(text="Hello")
        self.assertEqual(response.status, 201)
        post = await response.json()
        self.assertEqual(post["status"], "draft")
        self.assertEqual(post["accountIds"], ["id-bluesky"])
        self.app[KEYS["providers"]].publish.assert_not_called()

    async def test_accounts_resolve_by_id_handle_platform_name_and_all(self):
        for ref, expected in (
            ("id-mastodon", ["id-mastodon"]),
            ("@me.bsky.social", ["id-bluesky"]),
            ("me.bsky.social", ["id-bluesky"]),
            ("Mastodon", ["id-mastodon"]),
            ("all", ["id-bluesky", "id-mastodon"]),
        ):
            with self.subTest(ref=ref):
                post = await (await self.create(text="Hi", accounts=[ref])).json()
                self.assertEqual(sorted(post["accountIds"]), expected)
        post = await (await self.create(text="Hi", accounts="bluesky")).json()
        self.assertEqual(post["accountIds"], ["id-bluesky"])

    async def test_twitter_is_an_alias_for_x(self):
        await self.store.put("accounts", {"id": "id-x", "platform": "x", "name": "X (Twitter)"})
        post = await (await self.create(text="Hi", accounts=["twitter"])).json()
        self.assertEqual(post["accountIds"], ["id-x"])

    async def test_unknown_and_ambiguous_accounts_explain_the_options(self):
        response = await self.create(text="Hi", accounts=["linkedin"])
        self.assertEqual(response.status, 400)
        error = (await response.json())["error"]
        self.assertIn("No connected account matches 'linkedin'", error)
        self.assertIn("@me.bsky.social", error)
        await self.store.put(
            "accounts",
            {"id": "id-bluesky-2", "platform": "bluesky", "name": "Bluesky", "handle": "@two"},
        )
        response = await self.create(text="Hi", accounts=["bluesky"])
        self.assertEqual(response.status, 400)
        self.assertIn("matches several accounts", (await response.json())["error"])
        self.assertEqual((await self.create(text="Hi", accounts=["@two"])).status, 201)
        self.assertEqual((await self.create(text="Hi", accounts=[])).status, 400)
        self.assertEqual((await self.create(text="Hi", accounts=[7])).status, 400)

    async def test_scheduling_with_an_explicit_timezone(self):
        response = await self.create(text="Hi", scheduledAt=FUTURE, timezone="Asia/Jerusalem")
        self.assertEqual(response.status, 201)
        post = await response.json()
        self.assertEqual(post["status"], "scheduled")
        self.assertEqual(post["scheduledAt"], "2030-01-15T07:30:00.000Z")
        self.assertNotIn("notes", post)

    async def test_scheduling_with_an_offset_ignores_workspace_timezone(self):
        post = await (await self.create(text="Hi", scheduledAt=FUTURE + "+02:00")).json()
        self.assertEqual(post["scheduledAt"], "2030-01-15T07:30:00.000Z")
        self.assertNotIn("notes", post)

    async def test_scheduling_without_zone_uses_workspace_timezone_and_says_so(self):
        post = await (await self.create(text="Hi", scheduledAt=FUTURE)).json()
        self.assertEqual(post["scheduledAt"], "2030-01-15T09:30:00.000Z")
        self.assertIn("workspace timezone UTC", post["notes"][0])
        await self.store.put("settings", {"id": "workspace", "name": "W", "timezone": "Asia/Tokyo"})
        post = await (await self.create(text="Hi", scheduledAt=FUTURE)).json()
        self.assertEqual(post["scheduledAt"], "2030-01-15T00:30:00.000Z")
        self.assertIn("Asia/Tokyo", post["notes"][0])

    async def test_bad_times_are_rejected_with_guidance(self):
        for fields in (
            {"scheduledAt": "tomorrow at nine"},
            {"scheduledAt": 12345},
            {"scheduledAt": FUTURE, "timezone": "Mars/Base"},
            {"scheduledAt": FUTURE, "timezone": 5},
            {"scheduledAt": "2020-01-01T00:00:00Z"},
            {"status": "scheduled"},
        ):
            with self.subTest(fields=fields):
                self.assertEqual((await self.create(text="Hi", **fields)).status, 400)

    async def test_conflicting_status_and_schedule_is_rejected(self):
        for status in ("draft", "now"):
            response = await self.create(text="Hi", status=status, scheduledAt=FUTURE + "Z")
            self.assertEqual(response.status, 400)
            self.assertIn("requires status", (await response.json())["error"])
        self.assertEqual((await self.create(text="Hi", status="soon")).status, 400)

    async def test_misspelled_fields_are_rejected_instead_of_silently_dropped(self):
        response = await self.create(text="Hi", scheduled_at=FUTURE + "Z")
        self.assertEqual(response.status, 400)
        self.assertIn("scheduled_at", (await response.json())["error"])

    async def test_publish_now_delivers_through_the_scheduler(self):
        response = await self.create(text="Live", status="now")
        self.assertEqual(response.status, 202)
        post = await response.json()
        await self.jobs.tick()
        self.assertEqual(
            (await (await self.api("GET", "/posts/" + post["id"])).json())["status"], "published"
        )
        self.assertEqual((await self.create(text="Live", status="publish")).status, 202)

    async def test_x_still_requires_cost_confirmation(self):
        await self.store.put("accounts", {"id": "id-x", "platform": "x", "name": "X (Twitter)"})
        fields = {"text": "Hi", "accounts": ["x"], "status": "now"}
        response = await self.api("POST", "/posts", json=fields)
        self.assertEqual(response.status, 400)
        self.assertIn("X (Twitter) API cost", (await response.json())["error"])
        response = await self.api("POST", "/posts", json={**fields, "xCostConfirmed": True})
        self.assertEqual(response.status, 202)
        draft = await self.api("POST", "/posts", json={**fields, "status": "draft"})
        self.assertEqual(draft.status, 201)

    async def test_platform_rules_still_apply(self):
        response = await self.create(text="x" * 10001)
        self.assertEqual(response.status, 400)
        self.assertEqual((await self.create(text="   ")).status, 400)
        self.assertEqual(
            (
                await self.create(
                    text="Hi",
                    status="now",
                    contentOptions={"bluesky": {"text": False, "image": False}},
                )
            ).status,
            400,
        )

    async def test_content_options_accept_handles_and_platform_names(self):
        post = await (
            await self.create(
                text="Hi", accounts=["all"], contentOptions={"mastodon": {"image": False}}
            )
        ).json()
        self.assertEqual(post["contentOptions"], {"id-mastodon": {"image": False}})
        response = await self.create(text="Hi", contentOptions={"nobody": {"image": False}})
        self.assertEqual(response.status, 400)

    async def test_idempotency_key_prevents_duplicates(self):
        first = await self.create(text="Once", idempotencyKey="run-42", status="now")
        second = await self.create(text="Once", idempotencyKey="run-42", status="now")
        self.assertEqual((first.status, second.status), (202, 200))
        self.assertEqual((await first.json())["id"], (await second.json())["id"])
        self.assertEqual(len([p for p in await self.store.all("posts")]), 1)
        self.assertEqual((await self.create(text="Once", idempotencyKey="")).status, 400)
        self.assertEqual((await self.create(text="Once", idempotencyKey=5)).status, 400)

    async def test_update_keeps_omitted_fields_and_can_reschedule(self):
        original = await (
            await self.create(text="Original", accounts=["all"], scheduledAt=FUTURE + "Z")
        ).json()
        response = await self.api(
            "PUT", "/posts/" + original["id"], json={"scheduledAt": "2030-02-01T10:00:00Z"}
        )
        self.assertEqual(response.status, 201)
        updated = await response.json()
        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(updated["text"], "Original")
        self.assertEqual(updated["status"], "scheduled")
        self.assertEqual(updated["accountIds"], original["accountIds"])
        self.assertEqual(updated["scheduledAt"], "2030-02-01T10:00:00.000Z")
        updated = await (
            await self.api(
                "PUT", "/posts/" + original["id"], json={"text": "New", "status": "draft"}
            )
        ).json()
        self.assertEqual(
            (updated["text"], updated["status"], updated["scheduledAt"]), ("New", "draft", None)
        )

    async def test_updating_a_missing_or_delivered_post_fails(self):
        self.assertEqual((await self.api("PUT", "/posts/nope", json={"text": "x"})).status, 404)
        post = await (await self.create(text="Sent", status="now")).json()
        await self.jobs.tick()
        response = await self.api("PUT", "/posts/" + post["id"], json={"text": "Changed"})
        self.assertEqual(response.status, 400)

    async def test_cancel_retry_delete_and_listing(self):
        post = await (await self.create(text="Later", scheduledAt=FUTURE + "Z")).json()
        scheduled = await (await self.api("GET", "/posts?status=scheduled")).json()
        self.assertEqual([p["id"] for p in scheduled["posts"]], [post["id"]])
        self.assertEqual((await self.api("DELETE", "/posts/" + post["id"])).status, 400)
        cancelled = await (await self.api("POST", "/posts/" + post["id"] + "/cancel")).json()
        self.assertEqual(cancelled["status"], "draft")
        self.assertEqual((await self.api("POST", "/posts/" + post["id"] + "/retry")).status, 400)
        self.assertEqual((await self.api("DELETE", "/posts/" + post["id"])).status, 200)
        self.assertEqual((await self.api("GET", "/posts/" + post["id"])).status, 404)
        self.assertEqual((await (await self.api("GET", "/posts")).json())["total"], 0)
        self.assertEqual((await self.api("GET", "/posts?offset=abc")).status, 400)

    async def test_retry_accepts_an_empty_body(self):
        await self.store.put(
            "posts",
            {
                "id": "failed-post",
                "text": "x",
                "accountIds": ["id-bluesky"],
                "mediaIds": [],
                "status": "failed",
                "scheduledAt": None,
                "createdAt": now(),
                "results": [],
            },
        )
        response = await self.api("POST", "/posts/failed-post/retry")
        self.assertEqual(response.status, 202)
        self.assertEqual((await response.json())["status"], "queued")


class ImageTests(AutomationCase):
    def b64(self, content=PNG):
        return base64.b64encode(content).decode()

    async def test_base64_images_attach_with_alt_text(self):
        response = await self.create(text="Pic", images=[{"data": self.b64(), "alt": "A pixel"}])
        self.assertEqual(response.status, 201)
        post = await response.json()
        self.assertEqual(len(post["mediaIds"]), 1)
        media = await self.store.get("media", post["mediaIds"][0])
        self.assertEqual((media["mime"], media["alt"]), ("image/png", "A pixel"))
        self.assertTrue((self.jobs.media_dir / (media["id"] + ".bin")).exists())

    async def test_data_uri_and_image_only_posts(self):
        data = "data:image/png;base64," + self.b64()
        response = await self.create(images=[{"data": data}])
        self.assertEqual(response.status, 201)

    async def test_url_images_are_fetched_through_the_safe_transport(self):
        self.send.return_value = PNG
        post = await (
            await self.create(text="Pic", images=["https://example.com/pics/cat.png"])
        ).json()
        self.send.assert_awaited_once()
        self.assertEqual(self.send.await_args.args[:2], ("GET", "https://example.com/pics/cat.png"))
        media = await self.store.get("media", post["mediaIds"][0])
        self.assertEqual((media["name"], media["mime"]), ("cat.png", "image/png"))
        self.assertNotIn("sourceUrl", media)

    async def test_public_jpeg_urls_are_marked_for_instagram_and_threads(self):
        self.send.return_value = JPEG
        post = await (
            await self.create(text="Pic", images=[{"url": "https://example.com/a.jpg"}])
        ).json()
        media = await self.store.get("media", post["mediaIds"][0])
        self.assertEqual(media["sourceUrl"], "https://example.com/a.jpg")

    async def test_failed_downloads_explain_what_went_wrong_and_leave_nothing_behind(self):
        self.send.side_effect = ProviderError("Connection interrupted.")
        response = await self.create(text="Pic", images=["https://example.com/a.png"])
        self.assertEqual(response.status, 400)
        error = (await response.json())["error"]
        self.assertIn("Could not download the image from https://example.com/a.png", error)
        self.assertIn("base64", error)
        self.assertEqual(await self.store.all("media"), [])
        self.assertEqual(await self.store.all("posts"), [])

    async def test_private_and_malformed_sources_are_rejected_before_any_request(self):
        for spec in (
            {"url": "http://example.com/a.jpg"},
            {"url": "https://127.0.0.1/a.jpg"},
            {"url": "https://user:pw@example.com/a.jpg"},
            {"data": "not base64!!"},
            {"data": self.b64(b"GIF89a-not-supported")},
            {"data": 12},
            {},
            {"url": "https://example.com/a.jpg", "data": self.b64()},
            {"mediaId": "missing"},
            12,
        ):
            with self.subTest(spec=spec):
                response = await self.create(text="Pic", images=[spec])
                self.assertEqual(response.status, 400, await response.text())
        self.send.assert_not_called()
        self.assertEqual(await self.store.all("media"), [])

    async def test_oversized_images_are_rejected(self):
        big = PNG + b"0" * (1024 * 1024)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)  # aiohttp warns about 1 MB bodies.
            response = await self.create(text="Pic", images=[{"data": self.b64(big)}])
        self.assertEqual(response.status, 400)
        self.assertIn("1 MB", (await response.json())["error"])
        self.send.return_value = big
        self.assertEqual(
            (await self.create(text="Pic", images=["https://example.com/big.png"])).status, 400
        )
        self.assertEqual(await self.store.all("media"), [])

    async def test_more_than_four_images_is_rejected_without_fetching(self):
        images = [{"data": self.b64()}] * 5
        self.assertEqual((await self.create(text="Pic", images=images)).status, 400)
        self.assertEqual(await self.store.all("media"), [])

    async def test_failed_posts_do_not_leave_orphaned_images(self):
        response = await self.create(text="x" * 10001, images=[{"data": self.b64()}])
        self.assertEqual(response.status, 400)
        self.assertEqual(await self.store.all("media"), [])
        self.assertEqual(list(self.jobs.media_dir.iterdir()), [])
        # Images that already existed in the library are never deleted by a failed request.
        media = await (await self.api("POST", "/media", json={"data": self.b64()})).json()
        response = await self.create(text="x" * 10001, images=[{"mediaId": media["id"]}])
        self.assertEqual(response.status, 400)
        self.assertEqual(len(await self.store.all("media")), 1)

    async def test_idempotent_replays_do_not_download_or_keep_images(self):
        await self.create(text="Once", idempotencyKey="k1")
        self.send.return_value = PNG
        response = await self.create(
            text="Once", idempotencyKey="k1", images=["https://example.com/a.png"]
        )
        self.assertEqual(response.status, 200)
        self.send.assert_not_called()

    async def test_library_images_can_be_reused_and_given_alt_text(self):
        media = await (
            await self.api("POST", "/media", json={"data": self.b64(), "alt": "One"})
        ).json()
        self.assertEqual((media["alt"], media["publicSource"]), ("One", False))
        post = await (
            await self.create(text="Pic", images=[{"mediaId": media["id"], "alt": "Two"}])
        ).json()
        self.assertEqual(post["mediaIds"], [media["id"]])
        self.assertEqual((await self.store.get("media", media["id"]))["alt"], "Two")
        listing = await (await self.api("GET", "/media")).json()
        self.assertEqual([i["id"] for i in listing["images"]], [media["id"]])

    async def test_update_replaces_or_keeps_images(self):
        post = await (await self.create(text="Pic", images=[{"data": self.b64()}])).json()
        kept = await (await self.api("PUT", "/posts/" + post["id"], json={"text": "Edited"})).json()
        self.assertEqual(kept["mediaIds"], post["mediaIds"])
        cleared = await (await self.api("PUT", "/posts/" + post["id"], json={"images": []})).json()
        self.assertEqual(cleared["mediaIds"], [])

    async def test_multipart_upload_validates_type_and_size(self):
        form = aiohttp.FormData()
        form.add_field("file", PNG, filename="pixel.png", content_type="image/png")
        response = await self.api("POST", "/media?alt=Pixel", data=form)
        self.assertEqual(response.status, 201)
        media = await response.json()
        self.assertEqual(
            (media["name"], media["alt"], media["mime"]), ("pixel.png", "Pixel", "image/png")
        )
        form = aiohttp.FormData()
        form.add_field("file", b"%PDF-1.4 nope", filename="x.pdf")
        self.assertEqual((await self.api("POST", "/media", data=form)).status, 400)
        self.assertEqual((await self.api("DELETE", "/media/" + media["id"])).status, 200)
        self.assertEqual(await self.store.all("media"), [])

    async def test_an_image_in_a_post_cannot_be_deleted(self):
        post = await (await self.create(text="Pic", images=[{"data": self.b64()}])).json()
        response = await self.api("DELETE", "/media/" + post["mediaIds"][0])
        self.assertEqual(response.status, 400)

    async def test_published_posts_receive_the_stored_image_bytes(self):
        post = await (
            await self.create(
                text="Pic", status="now", images=[{"data": self.b64(), "alt": "Pixel"}]
            )
        ).json()
        await self.jobs.tick()
        publish = self.app[KEYS["providers"]].publish
        images = publish.await_args.args[2]
        self.assertEqual((images[0]["bytes"], images[0]["alt"]), (PNG, "Pixel"))
        self.assertEqual((await self.store.get("posts", post["id"]))["status"], "published")


class OpenApiTests(AutomationCase):
    async def test_every_documented_operation_is_routed(self):
        spec = await (await self.api("GET", "/openapi.json")).json()
        self.assertEqual(spec["openapi"], "3.1.0")
        routed = {
            (route.method, route.resource.canonical)
            for route in self.app.router.routes()
            if route.resource
        }
        for path, operations in spec["paths"].items():
            for method in operations:
                with self.subTest(path=path, method=method):
                    self.assertIn((method.upper(), path), routed)
        self.assertEqual(
            set(spec["components"]["schemas"]["NewPost"]["properties"]), set(POST_FIELDS)
        )

    async def test_server_url_follows_the_forwarded_scheme(self):
        headers = {**AUTH, "X-Forwarded-Proto": "https", "Host": "post.example.com"}
        spec = await (await self.api("GET", "/openapi.json", headers=headers)).json()
        self.assertEqual(spec["servers"], [{"url": "https://post.example.com"}])


class McpTests(AutomationCase):
    async def test_initialize_negotiates_version_and_describes_the_server(self):
        response = await self.rpc(
            "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}
        )
        result = (await response.json())["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "lightpost")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("draft", result["instructions"])
        result = (await (await self.rpc("initialize", {"protocolVersion": "1999-01-01"})).json())[
            "result"
        ]
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        self.assertEqual((await self.rpc("initialize", "oops")).status, 200)

    async def test_notifications_get_an_empty_accepted_response(self):
        response = await self.rpc("notifications/initialized", id=None)
        self.assertEqual(response.status, 202)
        self.assertEqual(await response.read(), b"")

    async def test_tools_list_matches_the_definitions(self):
        tools = (await (await self.rpc("tools/list")).json())["result"]["tools"]
        self.assertEqual(
            [t["name"] for t in tools],
            [
                "list_accounts",
                "create_post",
                "list_posts",
                "get_post",
                "cancel_post",
                "retry_post",
                "delete_post",
                "upload_image",
            ],
        )
        for tool in TOOLS:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertTrue(
                set(tool["inputSchema"]["required"]) <= set(tool["inputSchema"]["properties"])
            )
        create = next(t for t in tools if t["name"] == "create_post")
        self.assertEqual(set(POST_FIELDS) | {"postId"}, set(create["inputSchema"]["properties"]))
        self.assertTrue(
            next(t for t in tools if t["name"] == "delete_post")["annotations"]["destructiveHint"]
        )

    async def test_full_workflow_through_tools(self):
        accounts = await self.call_tool("list_accounts")
        self.assertFalse(accounts["isError"])
        self.assertNotIn("credentials", accounts["content"][0]["text"])
        import json

        listed = json.loads(accounts["content"][0]["text"])["accounts"]
        self.assertEqual({a["platform"] for a in listed}, {"bluesky", "mastodon"})

        created = await self.call_tool(
            "create_post",
            {
                "text": "From an agent",
                "accounts": ["bluesky", "mastodon"],
                "images": [{"data": base64.b64encode(PNG).decode(), "alt": "Pixel"}],
                "scheduledAt": FUTURE,
                "timezone": "Asia/Jerusalem",
            },
        )
        self.assertFalse(created["isError"], created)
        post = json.loads(created["content"][0]["text"])
        self.assertEqual(
            (post["status"], post["scheduledAt"]), ("scheduled", "2030-01-15T07:30:00.000Z")
        )

        moved = json.loads(
            (
                await self.call_tool(
                    "create_post", {"postId": post["id"], "scheduledAt": "2030-03-01T08:00:00Z"}
                )
            )["content"][0]["text"]
        )
        self.assertEqual(
            (moved["id"], moved["scheduledAt"], moved["text"]),
            (post["id"], "2030-03-01T08:00:00.000Z", "From an agent"),
        )

        page = json.loads(
            (await self.call_tool("list_posts", {"status": "scheduled"}))["content"][0]["text"]
        )
        self.assertEqual(page["total"], 1)
        fetched = json.loads(
            (await self.call_tool("get_post", {"postId": post["id"]}))["content"][0]["text"]
        )
        self.assertEqual(fetched["id"], post["id"])
        stopped = json.loads(
            (await self.call_tool("cancel_post", {"postId": post["id"]}))["content"][0]["text"]
        )
        self.assertEqual(stopped["status"], "draft")
        self.assertFalse((await self.call_tool("delete_post", {"postId": post["id"]}))["isError"])
        self.assertTrue((await self.call_tool("get_post", {"postId": post["id"]}))["isError"])

    async def test_upload_image_tool(self):
        import json

        result = await self.call_tool(
            "upload_image",
            {"data": base64.b64encode(PNG).decode(), "alt": "Pixel", "name": "p.png"},
        )
        media = json.loads(result["content"][0]["text"])
        self.assertEqual(
            (media["name"], media["alt"], media["mime"]), ("p.png", "Pixel", "image/png")
        )

    async def test_tool_failures_are_reported_to_the_model_not_as_protocol_errors(self):
        result = await self.call_tool("create_post", {"text": "Hi", "accounts": ["linkedin"]})
        self.assertTrue(result["isError"])
        self.assertIn("No connected account matches", result["content"][0]["text"])
        result = await self.call_tool(
            "create_post", {"text": "Hi", "accounts": ["bluesky"], "scheduled_at": "x"}
        )
        self.assertTrue(result["isError"])
        self.assertIn("Unknown field", result["content"][0]["text"])
        self.assertTrue((await self.call_tool("get_post", {}))["isError"])
        self.assertTrue((await self.call_tool("delete_post", {"postId": 5}))["isError"])
        self.assertTrue(
            (await self.call_tool("create_post", {"accounts": ["bluesky"], "postId": ""}))[
                "isError"
            ]
        )

    async def test_unexpected_errors_do_not_leak_details(self):
        with patch(
            "lightpost.api.live_accounts", AsyncMock(side_effect=RuntimeError("secret detail"))
        ):
            result = await self.call_tool("list_accounts")
        self.assertTrue(result["isError"])
        self.assertNotIn("secret detail", result["content"][0]["text"])

    async def test_protocol_errors(self):
        body = await (await self.rpc("tools/call", {"name": "nope"})).json()
        self.assertEqual(body["error"]["code"], -32602)
        body = await (await self.rpc("tools/call", {"name": "list_posts", "arguments": []})).json()
        self.assertEqual(body["error"]["code"], -32602)
        body = await (await self.rpc("resources/list")).json()
        self.assertEqual((body["error"]["code"], body["id"]), (-32601, 1))
        self.assertEqual((await (await self.rpc("ping")).json())["result"], {})
        bad = await self.client.post("/mcp", data="{nope", headers=AUTH)
        self.assertEqual((bad.status, (await bad.json())["error"]["code"]), (400, -32700))
        body = await (await self.client.post("/mcp", json=[1], headers=AUTH)).json()
        self.assertEqual(body[0]["error"]["code"], -32600)
        self.assertEqual((await self.client.post("/mcp", json=[], headers=AUTH)).status, 400)
        body = await (await self.client.post("/mcp", json={"id": 1}, headers=AUTH)).json()
        self.assertEqual(body["error"]["code"], -32600)

    async def test_batches_answer_requests_and_skip_notifications(self):
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        replies = await (await self.client.post("/mcp", json=batch, headers=AUTH)).json()
        self.assertEqual([r["id"] for r in replies], [1, 2])
        only_notes = [{"jsonrpc": "2.0", "method": "notifications/initialized"}]
        self.assertEqual(
            (await self.client.post("/mcp", json=only_notes, headers=AUTH)).status, 202
        )

    async def test_client_responses_are_accepted_silently(self):
        body = {"jsonrpc": "2.0", "id": 9, "result": {}}
        self.assertEqual((await self.client.post("/mcp", json=body, headers=AUTH)).status, 202)

    async def test_get_is_not_supported_and_trailing_slash_works(self):
        response = await self.client.get("/mcp", headers=AUTH)
        self.assertEqual((response.status, response.headers["Allow"]), (405, "POST"))
        response = await self.client.post(
            "/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=AUTH
        )
        self.assertEqual(response.status, 200)

    async def test_x_cost_confirmation_cannot_be_skipped_through_mcp(self):
        await self.store.put("accounts", {"id": "id-x", "platform": "x", "name": "X (Twitter)"})
        result = await self.call_tool(
            "create_post", {"text": "Hi", "accounts": ["x"], "status": "now"}
        )
        self.assertTrue(result["isError"])
        self.assertIn("X (Twitter) API cost", result["content"][0]["text"])
        self.assertEqual(await self.store.all("posts"), [])


class AutoKeyBase(AutomationCase):
    """Helpers for a fresh install, where LightPost creates and keeps its own key."""

    api_key = ""

    @property
    def key_file(self):
        return Path(self.directory.name) / "api.key"

    @property
    def key(self):
        return self.config.api_key

    def auth(self, key=None):
        return {"Authorization": "Bearer " + (key or self.key)}

    async def restart(self, **changes):
        await self.client.close()
        config = Config(
            database=self.config.database, key=KEY, scheduler=False, api_key="", **changes
        )
        self.client = TestClient(TestServer(create_app(config, self.send)))
        await self.client.start_server()
        self.config, self.app = config, self.client.server.app
        self.store = self.app[KEYS["store"]]


class AutoKeyTests(AutoKeyBase):
    async def test_key_is_created_privately_and_works_immediately(self):
        self.assertTrue(self.key.startswith("lp_"))
        self.assertGreaterEqual(len(self.key), 40)
        self.assertEqual(self.key_file.read_text().strip(), self.key)
        self.assertEqual(stat.S_IMODE(self.key_file.stat().st_mode), 0o600)
        response = await self.client.get("/api/v1/accounts", headers=self.auth())
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.client.get("/api/v1/accounts")).status, 401)

    async def test_key_survives_restarts(self):
        first = self.key
        await self.restart()
        self.assertEqual(self.key, first)
        response = await self.client.get("/api/v1/accounts", headers=self.auth(first))
        self.assertEqual(response.status, 200)

    async def test_damaged_key_file_is_replaced(self):
        await self.client.close()
        self.key_file.write_text("oops")
        await self.restart()
        self.assertGreaterEqual(len(self.key), 40)
        self.assertEqual(self.key_file.read_text().strip(), self.key)

    async def test_settings_page_can_read_the_key_but_only_with_a_ui_session(self):
        info = await (await self.client.get("/api/automation")).json()
        self.assertEqual(
            info,
            {
                "enabled": True,
                "locked": False,
                "source": "file",
                "envIgnored": False,
                "key": self.key,
            },
        )
        headers = {"Host": "lightpost.example.com"}
        self.assertEqual((await self.client.get("/api/automation", headers=headers)).status, 403)

    async def test_regenerating_replaces_the_key_everywhere(self):
        old = self.key
        response = await self.client.post("/api/automation/regenerate")
        self.assertEqual(response.status, 200)
        new = (await response.json())["key"]
        self.assertNotEqual(new, old)
        self.assertEqual(self.key_file.read_text().strip(), new)
        self.assertEqual(stat.S_IMODE(self.key_file.stat().st_mode), 0o600)
        self.assertEqual(
            (await self.client.get("/api/v1/accounts", headers=self.auth(old))).status, 401
        )
        self.assertEqual(
            (await self.client.get("/api/v1/accounts", headers=self.auth(new))).status, 200
        )
        await self.restart()
        self.assertEqual(self.key, new)

    async def test_switch_turns_access_off_and_on_and_is_remembered(self):
        off = await self.client.put("/api/automation", json={"enabled": False})
        self.assertFalse((await off.json())["enabled"])
        for request in (
            self.client.get("/api/v1/accounts", headers=self.auth()),
            self.client.post("/mcp", json={}, headers=self.auth()),
            self.client.post(f"/mcp/{self.key}", json={}),
        ):
            response = await request
            self.assertEqual(response.status, 403)
            self.assertIn("Settings", (await response.json())["error"])
        await self.restart()
        self.assertEqual(
            (await self.client.get("/api/v1/accounts", headers=self.auth())).status, 403
        )
        await self.client.put("/api/automation", json={"enabled": True})
        self.assertEqual(
            (await self.client.get("/api/v1/accounts", headers=self.auth())).status, 200
        )
        for bad in ({"enabled": "yes"}, {}):
            self.assertEqual((await self.client.put("/api/automation", json=bad)).status, 400)

    async def start_with_environment_key(self):
        await self.client.close()
        config = Config(database=self.config.database, key=KEY, scheduler=False, api_key=API_KEY)
        self.client = TestClient(TestServer(create_app(config, self.send)))
        await self.client.start_server()

    async def test_environment_key_wins_by_default(self):
        await self.start_with_environment_key()
        info = await (await self.client.get("/api/automation")).json()
        self.assertEqual(
            (info["source"], info["key"], info["envIgnored"]), ("environment", API_KEY, False)
        )
        self.assertEqual((await self.client.get("/api/v1/accounts", headers=AUTH)).status, 200)

    async def test_regenerating_an_environment_key_hands_control_to_lightpost(self):
        await self.start_with_environment_key()
        response = await self.client.post("/api/automation/regenerate")
        self.assertEqual(response.status, 200)
        info = await response.json()
        new = info["key"]
        self.assertNotIn(new, (API_KEY, ""))
        self.assertEqual((info["source"], info["envIgnored"]), ("file", True))
        self.assertEqual(self.key_file.read_text().strip(), new)
        # The old .env key stops working at once, the new one works.
        self.assertEqual((await self.client.get("/api/v1/accounts", headers=AUTH)).status, 401)
        self.assertEqual(
            (await self.client.get("/api/v1/accounts", headers=self.auth(new))).status, 200
        )
        # After a restart the API_KEY line in .env is still ignored.
        await self.start_with_environment_key()
        info = await (await self.client.get("/api/automation")).json()
        self.assertEqual((info["key"], info["source"], info["envIgnored"]), (new, "file", True))
        self.assertEqual((await self.client.get("/api/v1/accounts", headers=AUTH)).status, 401)
        # Regenerating again keeps working, and the on/off switch keeps the choice.
        await self.client.put("/api/automation", json={"enabled": False})
        await self.client.put("/api/automation", json={"enabled": True})
        await self.start_with_environment_key()
        info = await (await self.client.get("/api/automation")).json()
        self.assertEqual((info["key"], info["envIgnored"]), (new, True))

    async def test_connector_address_carries_the_key_for_tools_without_headers(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        response = await self.client.post(f"/mcp/{self.key}", json=body)
        self.assertEqual(response.status, 200)
        self.assertEqual(len((await response.json())["result"]["tools"]), 8)
        self.assertEqual((await self.client.post(f"/mcp/{self.key}/", json=body)).status, 200)
        self.assertEqual((await self.client.post("/mcp/wrong-key", json=body)).status, 401)
        self.assertEqual((await self.client.get(f"/mcp/{self.key}")).status, 405)
        # The header still works on the plain address, and a wrong header is not rescued by the URL.
        self.assertEqual(
            (await self.client.post("/mcp", json=body, headers=self.auth())).status, 200
        )
        self.assertEqual((await self.client.post("/mcp", json=body)).status, 401)

    async def test_similarly_named_pages_are_not_mistaken_for_the_api(self):
        self.assertEqual((await self.client.get("/mcpx")).status, 404)
        self.assertEqual((await self.client.get("/api/v1x")).status, 404)

    async def test_api_key_command_prints_the_saved_key(self):
        from lightpost.__main__ import main

        env = {"DATABASE_PATH": self.config.database, "API_KEY": ""}
        with patch.dict(os.environ, env), patch("sys.argv", ["lightpost", "api-key"]):
            with patch("sys.stdout", new=StringIO()) as out:
                await asyncio.to_thread(main)
        self.assertEqual(out.getvalue().strip(), self.key)


class PasswordAutomationTests(AutoKeyBase):
    password = "p" * 20

    async def test_key_is_hidden_from_visitors_who_have_not_signed_in(self):
        self.assertEqual((await self.client.get("/api/automation")).status, 401)
        self.assertEqual((await self.client.post("/api/automation/regenerate")).status, 401)
        response = await self.client.put("/api/automation", json={"enabled": False})
        self.assertEqual(response.status, 401)
        # The API key does not open the settings page either.
        response = await self.client.get("/api/automation", headers=self.auth())
        self.assertEqual(response.status, 401)

    async def test_signed_in_owner_can_see_the_key(self):
        await self.client.post("/api/login", json={"password": self.password})
        info = await (await self.client.get("/api/automation")).json()
        self.assertEqual(info["key"], self.key)


if __name__ == "__main__":
    unittest.main()
