import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from lightpost.app import KEYS, create_app
from lightpost.config import Config
from lightpost.providers import ProviderError
from lightpost.youtube import API, SCOPES, TOKEN_URL, UPLOAD_URL, refresh, session_url

KEY = "a1" * 32
CLIENT = "test-client.apps.googleusercontent.com"
CHANNEL = "UC" + "a" * 22
VIDEO = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 28
SESSION = UPLOAD_URL + "?upload_id=test-only-session"
REMOTE_ID = "a1b2c3d4e5f"


class YoutubeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.send = AsyncMock(side_effect=self.response)
        self.client = TestClient(
            TestServer(
                create_app(
                    Config(
                        database=str(Path(self.directory.name) / "test.sqlite"),
                        key=KEY,
                        scheduler=False,
                    ),
                    self.send,
                )
            )
        )
        await self.client.start_server()
        self.app = self.client.server.app
        self.jobs = self.app[KEYS["jobs"]]
        self.store = self.app[KEYS["store"]]

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def response(self, method, url, **kwargs):
        if url == TOKEN_URL:
            return {
                "access_token": "dummy-access",
                "refresh_token": "dummy-refresh",
                "scope": SCOPES,
                "expires_in": 3600,
            }
        if url == API + "/channels":
            return {"items": [{"id": CHANNEL, "snippet": {"title": "Test channel"}}]}
        if url == UPLOAD_URL:
            return {"status": 200, "headers": {"Location": SESSION}, "data": {}}
        if url == SESSION:
            if kwargs.get("data") == b"":
                return {"status": 308, "headers": {}, "data": {}}
            return {"status": 200, "headers": {}, "data": {"id": REMOTE_ID}}
        if url == API + "/videos":
            return {
                "items": [
                    {
                        "id": REMOTE_ID,
                        "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"},
                    }
                ]
            }
        self.fail(f"Unexpected request: {method} {url}")

    async def connect(self, **changes):
        response = await self.client.post(
            "/api/accounts",
            json={
                "platform": "youtube",
                "clientId": CLIENT,
                "clientSecret": "dummy-client-secret",
                "secret": "dummy-refresh",
                **changes,
            },
        )
        self.assertEqual(response.status, 201, await response.text())
        return await response.json()

    async def video(self, content=VIDEO, name="test.mp4"):
        body = aiohttp.FormData()
        body.add_field("file", content, filename=name, content_type="video/mp4")
        return await self.client.post("/api/videos", data=body)

    async def post(self, **changes):
        account = await self.connect()
        video = await (await self.video()).json()
        response = await self.client.post(
            "/api/posts",
            json={
                "text": "",
                "accountIds": [account["id"]],
                "mediaIds": [],
                "videoId": video["id"],
                "status": "now",
                "youtube": {
                    "title": "Test video",
                    "visibility": "unlisted",
                    "madeForKids": False,
                    "syntheticMedia": False,
                },
                **changes,
            },
        )
        return response

    async def oauth(self, **changes):
        response = await self.client.post(
            "/api/youtube/start",
            json={
                "clientConfig": {
                    "installed": {
                        "client_id": CLIENT,
                        "client_secret": "dummy-client-secret",
                        "token_uri": "https://untrusted.example/token",
                    }
                },
                **changes,
            },
        )
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        return parse_qs(urlsplit(data["url"]).query)

    async def test_connect_stores_encrypted_tokens_without_returning_secrets(self):
        account = await self.connect()
        self.assertNotIn("dummy-", str(account))
        saved = await self.store.get("accounts", account["id"])
        self.assertNotIn("dummy-", saved["credentials"])
        defaults = await (await self.client.get(f"/api/accounts/{account['id']}/connection")).json()
        self.assertEqual(defaults["values"], {})
        self.assertEqual(set(defaults["savedFields"]), {"clientId", "clientSecret", "secret"})
        await self.connect(id=account["id"], clientId="", clientSecret="", secret="")
        exported = await (await self.client.get("/api/export")).text()
        self.assertNotIn("dummy-", exported)

    async def test_cached_token_survives_storage_and_processing_checks(self):
        response = await self.post()
        post = await response.json()
        self.send.reset_mock()
        await self.jobs.deliver(post["id"])
        await self.jobs.deliver(post["id"])
        self.assertFalse(any(call.args[1] == TOKEN_URL for call in self.send.call_args_list))
        account = (await self.store.all("accounts"))[0]
        credentials = self.app[KEYS["vault"]].decrypt(account["credentials"])
        self.assertGreater(credentials["tokenExpiresAt"], time.time() + 60)
        self.assertEqual((await self.store.get("posts", post["id"]))["status"], "published")

    async def test_token_refreshes_one_minute_before_expiry_and_retains_refresh_token(self):
        providers = self.app[KEYS["providers"]]
        credentials = {
            "clientId": CLIENT,
            "clientSecret": "dummy",
            "refreshToken": "original-refresh",
            "token": "old-access",
            "tokenExpiresAt": 1061,
        }
        with patch("lightpost.youtube.time.time", return_value=1000):
            self.assertIs(await refresh(providers, credentials), credentials)
            self.send.assert_not_called()
        self.send.side_effect = None
        self.send.return_value = {"access_token": "new-access", "expires_in": 3600}
        with patch("lightpost.youtube.time.time", return_value=1001):
            updated = await refresh(providers, credentials)
        self.assertEqual(updated["tokenExpiresAt"], 4601)
        self.assertEqual(updated["token"], "new-access")
        self.assertEqual(updated["refreshToken"], "original-refresh")
        self.assertEqual(self.send.call_count, 1)

    async def test_unknown_expiry_is_not_cached_and_expired_grant_requests_reconnect(self):
        providers = self.app[KEYS["providers"]]
        credentials = {
            "clientId": CLIENT,
            "clientSecret": "dummy",
            "refreshToken": "original-refresh",
            "token": "old-access",
        }
        self.send.side_effect = None
        self.send.return_value = {"access_token": "new-access", "expires_in": "bad-value"}
        updated = await refresh(providers, credentials)
        self.assertLessEqual(updated["tokenExpiresAt"], time.time())
        self.send.side_effect = ProviderError("dummy expired grant", status=400)
        with self.assertRaisesRegex(ProviderError, "Reconnect"):
            await refresh(providers, updated)
        self.assertEqual(updated["refreshToken"], "original-refresh")

    async def expire_account(self):
        account = (await self.store.all("accounts"))[0]
        vault = self.app[KEYS["vault"]]
        credentials = vault.decrypt(account["credentials"])
        credentials["tokenExpiresAt"] = 0
        await self.store.put("accounts", {**account, "credentials": vault.encrypt(credentials)})
        return account

    async def test_expired_authorization_blocks_schedule_and_preserves_draft(self):
        post = await (await self.post(status="draft")).json()
        account = await self.expire_account()
        self.send.side_effect = ProviderError("dummy invalid grant", status=400)
        response = await self.client.post(
            "/api/posts",
            json={
                **post,
                "status": "scheduled",
                "scheduledAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            },
        )
        self.assertEqual(response.status, 400)
        error = await response.json()
        self.assertEqual(error["reconnectAccountIds"], [account["id"]])
        self.assertIn("Reconnect", error["error"])
        self.assertEqual((await self.store.get("posts", post["id"]))["status"], "draft")
        state = await (await self.client.get("/api/state")).json()
        self.assertEqual(state["accounts"][0]["connectionStatus"], "reconnect_required")
        self.assertNotIn("dummy-refresh", str(state))
        self.send.side_effect = self.response
        await self.connect(id=account["id"])
        state = await (await self.client.get("/api/state")).json()
        self.assertNotEqual(state["accounts"][0].get("connectionStatus"), "reconnect_required")

    async def test_transient_google_failure_does_not_mark_account_expired(self):
        post = await (await self.post(status="draft")).json()
        await self.expire_account()
        self.send.side_effect = ProviderError("dummy network interruption", uncertain=True)
        response = await self.client.post("/api/posts", json={**post, "status": "now"})
        self.assertEqual(response.status, 400)
        error = await response.json()
        self.assertNotIn("reconnectAccountIds", error)
        self.assertIn("Try again", error["error"])
        account = (await self.store.all("accounts"))[0]
        self.assertNotEqual(account.get("connectionStatus"), "reconnect_required")
        self.assertEqual((await self.store.get("posts", post["id"]))["status"], "draft")

    async def test_later_delivery_expiry_marks_account_and_retry_requires_reconnect(self):
        post = await (await self.post()).json()
        account = await self.expire_account()
        self.send.side_effect = ProviderError("dummy invalid grant", status=400)
        await self.jobs.deliver(post["id"])
        saved = await self.store.get("posts", post["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertIn("Reconnect", saved["results"][-1]["message"])
        self.assertEqual(
            (await self.store.get("accounts", account["id"]))["connectionStatus"],
            "reconnect_required",
        )
        response = await self.client.post(f"/api/posts/{post['id']}/retry", json={})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["reconnectAccountIds"], [account["id"]])

    async def test_drafts_can_be_saved_without_google_authorization(self):
        post = await (await self.post(status="draft")).json()
        await self.expire_account()
        self.send.reset_mock()
        self.send.side_effect = ProviderError("dummy invalid grant", status=400)
        response = await self.client.post(
            "/api/posts", json={**post, "text": "Keep my edits", "status": "draft"}
        )
        self.assertEqual(response.status, 201)
        self.send.assert_not_called()

    async def test_oauth_pkce_cookie_and_one_time_state(self):
        query = await self.oauth()
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["scope"], [SCOPES])
        state = query["state"][0]
        self.assertNotIn("dummy-client-secret", str(query))
        path = "/oauth/youtube/callback?" + "state=" + state + "&code=dummy-code"
        response = await self.client.get(path)
        self.assertEqual(response.status, 200, await response.text())
        self.assertIn("YouTube connected", await response.text())
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual((await self.client.get(path)).status, 400)
        self.assertTrue(
            all("untrusted.example" not in call.args[1] for call in self.send.call_args_list)
        )
        exchange = self.send.call_args_list[0].kwargs["data"]
        self.assertEqual(exchange["grant_type"], "authorization_code")
        self.assertIn("code_verifier", exchange)

    async def test_oauth_requires_same_browser_and_unexpired_state(self):
        query = await self.oauth()
        state = query["state"][0]
        self.client.session.cookie_jar.clear()
        response = await self.client.get(f"/oauth/youtube/callback?state={state}&code=test")
        self.assertEqual(response.status, 400)
        self.send.assert_not_called()
        query = await self.oauth()
        self.app[KEYS["youtube_oauth"]][query["state"][0]]["expires"] = 0
        self.assertEqual(
            (
                await self.client.get(
                    f"/oauth/youtube/callback?state={query['state'][0]}&code=test"
                )
            ).status,
            400,
        )

    async def test_oauth_rejects_partial_grant_and_wrong_client_type(self):
        self.send.side_effect = None
        self.send.return_value = {
            "access_token": "dummy",
            "refresh_token": "dummy",
            "scope": "https://www.googleapis.com/auth/youtube.readonly",
        }
        query = await self.oauth()
        response = await self.client.get(
            f"/oauth/youtube/callback?state={query['state'][0]}&code=test"
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(await self.store.all("accounts"), [])
        response = await self.client.post(
            "/api/youtube/start", json={"clientConfig": {"web": {"client_id": CLIENT}}}
        )
        self.assertEqual(response.status, 400)

    async def test_oauth_refuses_remote_origin_and_cross_site_writes(self):
        self.app[KEYS["config"]].password = "dummy-workspace-password"
        self.app[KEYS["sessions"]]["dummy-session"] = time.monotonic() + 100
        self.client.session.cookie_jar.update_cookies({"lightpost_session": "dummy-session"})
        data = {"clientConfig": {"installed": {"client_id": CLIENT, "client_secret": "dummy"}}}
        response = await self.client.post(
            "/api/youtube/start", json=data, headers={"Host": "public.example.com"}
        )
        self.assertEqual(response.status, 400)
        response = await self.client.post(
            "/api/youtube/start", json=data, headers={"Origin": "https://other.example.com"}
        )
        self.assertEqual(response.status, 403)

    async def test_video_is_private_local_range_streamed_and_validated(self):
        response = await self.video()
        self.assertEqual(response.status, 201)
        video = await response.json()
        path = self.jobs.media_dir / (video["id"] + ".mp4")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        response = await self.client.get(
            "/api/videos/" + video["id"], headers={"Range": "bytes=0-7"}
        )
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), VIDEO[:8])
        self.assertEqual((await self.video(content=b"not a video")).status, 400)
        self.assertEqual((await self.video(name="bad.mov")).status, 400)
        with patch("lightpost.youtube.MAX_VIDEO", 20):
            self.assertEqual((await self.video()).status, 400)
        self.assertEqual(len(list(self.jobs.media_dir.glob("*.mp4"))), 1)

    async def test_draft_and_schedule_and_used_video_cannot_be_deleted(self):
        response = await self.post(
            status="scheduled",
            scheduledAt=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        self.assertEqual(response.status, 201, await response.text())
        post = await response.json()
        self.assertEqual(post["status"], "scheduled")
        self.assertEqual((await self.client.delete("/api/videos/" + post["videoId"])).status, 400)
        self.send.reset_mock()
        await self.jobs.deliver(post["id"])
        self.send.assert_not_called()

    async def test_video_delivery_requires_explicit_metadata_and_youtube_only(self):
        response = await self.post(youtube={"title": "Test", "visibility": "unlisted"})
        self.assertEqual(response.status, 400)
        account = (await self.store.all("accounts"))[0]
        await self.store.put("accounts", {**account, "id": "other", "platform": "bluesky"})
        video = (await self.store.all("videos"))[0]
        response = await self.client.post(
            "/api/posts",
            json={
                "text": "hello",
                "accountIds": ["other"],
                "videoId": video["id"],
                "status": "draft",
            },
        )
        self.assertEqual(response.status, 400)

    async def test_delivery_persists_encrypted_session_then_checks_processing(self):
        response = await self.post()
        self.assertEqual(response.status, 202, await response.text())
        post = await response.json()
        await self.jobs.deliver(post["id"])
        saved = await self.store.get("posts", post["id"])
        self.assertEqual(saved["status"], "processing")
        self.assertNotIn("upload_id", str(saved))
        self.assertNotIn("dummy-access", str(saved))
        self.assertEqual(saved["results"][-1]["stage"], "verifying")
        create = next(call for call in self.send.call_args_list if call.args[1] == UPLOAD_URL)
        self.assertEqual(create.kwargs["json"]["status"]["privacyStatus"], "unlisted")
        self.assertIs(create.kwargs["json"]["status"]["selfDeclaredMadeForKids"], False)
        await self.jobs.deliver(post["id"])
        saved = await self.store.get("posts", post["id"])
        self.assertEqual(saved["status"], "published")
        self.assertTrue(saved["results"][-1]["url"].endswith(REMOTE_ID))
        self.assertEqual(sum(call.args[1] == UPLOAD_URL for call in self.send.call_args_list), 1)

    async def test_interruption_resumes_session_and_restart_recovers_sending(self):
        response = await self.post()
        post = await response.json()
        original = self.response

        async def interrupted(method, url, **kwargs):
            if url == SESSION:
                raise ProviderError("network failed", uncertain=True)
            return await original(method, url, **kwargs)

        self.send.side_effect = interrupted
        await self.jobs.deliver(post["id"])
        saved = await self.store.get("posts", post["id"])
        latest = saved["results"][-1]
        self.assertEqual(latest["stage"], "youtube-upload")
        self.assertNotIn("upload_id", latest["containerId"])
        # Simulate a restart after the scheduler records sending but before another receipt.
        saved["status"] = "publishing"
        saved["results"].append({**latest, "status": "sending"})
        await self.store.put("posts", saved)
        await self.jobs.recover()
        self.assertEqual((await self.store.get("posts", post["id"]))["status"], "processing")
        self.send.side_effect = original
        await self.jobs.deliver(post["id"])
        self.assertEqual(sum(call.args[1] == UPLOAD_URL for call in self.send.call_args_list), 1)

    async def test_expired_upload_and_visibility_mismatch_never_start_duplicate(self):
        response = await self.post()
        post = await response.json()
        original = self.response

        async def expired(method, url, **kwargs):
            if url == SESSION:
                raise ProviderError("expired", status=410)
            return await original(method, url, **kwargs)

        self.send.side_effect = expired
        await self.jobs.deliver(post["id"])
        self.assertEqual((await self.store.get("posts", post["id"]))["status"], "needs_review")
        await self.client.post(f"/api/posts/{post['id']}/retry", json={})
        self.send.side_effect = original
        await self.jobs.deliver(post["id"])

        async def mismatch(method, url, **kwargs):
            if url == API + "/videos":
                return {
                    "items": [{"status": {"uploadStatus": "processed", "privacyStatus": "private"}}]
                }
            return await original(method, url, **kwargs)

        self.send.side_effect = mismatch
        await self.jobs.deliver(post["id"])
        self.assertEqual((await self.store.get("posts", post["id"]))["status"], "failed")
        self.assertEqual(sum(call.args[1] == UPLOAD_URL for call in self.send.call_args_list), 1)

    async def test_disconnected_or_wrong_channel_cannot_replace_identity(self):
        account = await self.connect()
        original = self.response

        async def other_channel(method, url, **kwargs):
            if url == API + "/channels":
                return {"items": [{"id": "UC" + "b" * 22, "snippet": {"title": "Other"}}]}
            return await original(method, url, **kwargs)

        self.send.side_effect = other_channel
        response = await self.client.post(
            "/api/accounts",
            json={
                "id": account["id"],
                "platform": "youtube",
                "clientId": CLIENT,
                "clientSecret": "dummy",
                "secret": "dummy",
            },
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await self.store.get("accounts", account["id"]))["remoteId"], CHANNEL)

    def test_upload_session_cannot_redirect_credentials_to_another_host(self):
        self.assertEqual(session_url(SESSION), SESSION)
        for url in (
            "https://evil.example/upload?x=1",
            "https://www.googleapis.com.evil.example/upload/youtube/v3/videos?x=1",
            "http://www.googleapis.com/upload/youtube/v3/videos?x=1",
            "https://www.googleapis.com/other?x=1",
        ):
            with self.assertRaises(ProviderError):
                session_url(url)
