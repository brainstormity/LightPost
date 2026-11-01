import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import UUID

from aiohttp.test_utils import TestClient, TestServer

from lightpost.app import KEYS, create_app
from lightpost.config import Config

KEY = "b7" * 32  # Test-only key. All requests are intercepted by mock_transport.
TOKEN = "TEST_ONLY_META_ACCESS_TOKEN"
INSTAGRAM_USER_ID = "100000001"
FACEBOOK_PAGE_ID = "100000002"
INSTAGRAM_CONTAINER_ID = "700000001"
INSTAGRAM_POST_ID = "800000001"
FACEBOOK_POST_ID = FACEBOOK_PAGE_ID + "_800000002"
PUBLIC_IMAGE_URL = "https://example.org/first-comment-e2e.jpg"
JPEG = b"\xff\xd8\xffmocked-public-image"


class FirstCommentE2ETests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.draft_sequence = 0
        self.transport_calls = []
        self.comment_publication_snapshots = []
        self.instagram_container_status = "FINISHED"
        self.facebook_confirmed = True
        self.transport = AsyncMock(side_effect=self.mock_transport)
        config = Config(
            database=str(Path(self.directory.name) / "first-comments.sqlite"),
            key=KEY,
            scheduler=False,
        )
        self.client = TestClient(TestServer(create_app(config, self.transport)))
        await self.client.start_server()
        app = self.client.server.app
        self.store = app[KEYS["store"]]
        self.jobs = app[KEYS["jobs"]]
        self.vault = app[KEYS["vault"]]
        app[KEYS["providers"]].pause = AsyncMock()

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def mock_transport(self, method, url, **kwargs):
        self.transport_calls.append((method, url, kwargs))
        parts = urlsplit(url)
        if kwargs.get("_binary"):
            self.assertEqual(url, PUBLIC_IMAGE_URL)
            return JPEG

        path = parts.path
        if method == "POST" and path.endswith("/comments"):
            remote_post_id = path.rsplit("/", 2)[-2]
            matches = []
            for post in await self.store.all("posts"):
                receipt = next(
                    (
                        value
                        for value in reversed(post.get("results", []))
                        if value.get("remoteId") == remote_post_id
                    ),
                    None,
                )
                if receipt:
                    matches.append((post, receipt))
            self.comment_publication_snapshots.append(
                {
                    "remotePostId": remote_post_id,
                    "matches": len(matches),
                    "postId": matches[0][0]["id"] if len(matches) == 1 else None,
                    "parentStatus": matches[0][0].get("status") if len(matches) == 1 else None,
                    "accountId": matches[0][1].get("accountId") if len(matches) == 1 else None,
                    "receiptStatus": matches[0][1].get("status") if len(matches) == 1 else None,
                }
            )
            if parts.netloc == "graph.instagram.com":
                return {"id": "900000001"}
            if parts.netloc == "graph.facebook.com":
                return {"id": "900000002"}

        if parts.netloc == "graph.instagram.com":
            if method == "POST" and path.endswith(f"/{INSTAGRAM_USER_ID}/media"):
                return {"id": INSTAGRAM_CONTAINER_ID}
            if method == "GET" and path.endswith("/" + INSTAGRAM_CONTAINER_ID):
                return {"status_code": self.instagram_container_status}
            if method == "POST" and path.endswith(f"/{INSTAGRAM_USER_ID}/media_publish"):
                return {"id": INSTAGRAM_POST_ID}
            if method == "GET" and path.endswith("/" + INSTAGRAM_POST_ID):
                return {"permalink": "https://www.instagram.com/p/mock-post/"}

        if parts.netloc == "graph.facebook.com":
            if method == "POST" and path.endswith(f"/{FACEBOOK_PAGE_ID}/feed"):
                return {"id": FACEBOOK_POST_ID}
            if method == "GET" and path.endswith("/" + FACEBOOK_POST_ID):
                return {"is_published": self.facebook_confirmed}

        self.fail(f"Unexpected mocked request: {method} {url}")

    async def add_account(self, platform):
        if platform == "instagram":
            account = {
                "id": "ig-account",
                "name": "Test Instagram",
                "platform": "instagram",
                "remoteId": INSTAGRAM_USER_ID,
                "demo": False,
                "credentials": self.vault.encrypt(
                    {"token": TOKEN, "userId": INSTAGRAM_USER_ID, "lastRefreshAt": time.time()}
                ),
            }
        elif platform == "facebook":
            account = {
                "id": "fb-account",
                "name": "Test Facebook Page",
                "platform": "facebook",
                "remoteId": FACEBOOK_PAGE_ID,
                "demo": False,
                "credentials": self.vault.encrypt({"token": TOKEN, "pageId": FACEBOOK_PAGE_ID}),
            }
        else:
            self.fail(f"Unsupported fixture platform: {platform}")
        await self.store.put("accounts", account)
        return account["id"]

    async def add_public_image(self):
        response = await self.client.post("/api/media/url", json={"url": PUBLIC_IMAGE_URL})
        self.assertEqual(response.status, 201, await response.text())
        return (await response.json())["id"]

    async def save_draft(self, *, account_ids, first_comments=None, media_ids=None, options=None):
        payload = {
            "text": "A mocked post for first-comment coverage.",
            "accountIds": account_ids,
            "mediaIds": media_ids or [],
            "status": "draft",
        }
        if first_comments is not None:
            payload["firstComments"] = first_comments
        if options:
            payload["contentOptions"] = options
        self.draft_sequence += 1
        draft_id = UUID(f"12345678-1234-5678-1234-{self.draft_sequence:012d}")
        with patch("lightpost.app.uuid4", return_value=draft_id):
            response = await self.client.post("/api/posts", json=payload)
        self.assertEqual(response.status, 201, await response.text())
        saved = await response.json()
        persisted = await self.store.get("posts", saved["id"])
        return payload, saved, persisted

    async def publish_draft(self, composer_payload, saved_draft):
        response = await self.client.post(
            "/api/posts",
            json={**composer_payload, "id": saved_draft["id"], "status": "now"},
        )
        self.assertEqual(response.status, 202, await response.text())
        await self.jobs.deliver(saved_draft["id"])
        return await self.store.get("posts", saved_draft["id"])

    def social_calls(self):
        return [
            (method, url, kwargs)
            for method, url, kwargs in self.transport_calls
            if urlsplit(url).netloc in ("graph.instagram.com", "graph.facebook.com")
        ]

    def comment_calls(self):
        return [
            (method, url, kwargs)
            for method, url, kwargs in self.social_calls()
            if method == "POST" and urlsplit(url).path.endswith("/comments")
        ]

    async def assert_comment_sent(self, post_id, account_id):
        comments = await self.jobs.first_comment_results(post_id)
        receipt = next((item for item in comments if item["accountId"] == account_id), None)
        self.assertIsNotNone(receipt, comments)
        self.assertEqual(receipt["status"], "sent", receipt)
        self.assertTrue(receipt.get("remoteCommentId"), receipt)
        return receipt

    def print_trace(self, *, platform, composer, draft, published, account_id, expected_text):
        comment = next(
            call
            for call in self.comment_calls()
            if urlsplit(call[1]).netloc
            == ("graph.instagram.com" if platform == "instagram" else "graph.facebook.com")
        )
        method, url, kwargs = comment
        receipt = next(
            result
            for result in reversed(published["results"])
            if result["accountId"] == account_id and result["status"] == "published"
        )
        trace = {
            "execution": "MOCKED; composer JSON -> persisted API draft -> actual scheduler/provider -> intercepted Graph request; no browser or real social calls",
            "platform": platform,
            "composer_input": {"firstComments": composer["firstComments"]},
            "saved_draft": {
                "id": draft["id"],
                "status": draft["status"],
                "firstComments": draft["firstComments"],
            },
            "confirmed_publication": {
                "accountId": receipt["accountId"],
                "status": receipt["status"],
                "remoteId": receipt["remoteId"],
            },
            "graph_comment_post": {
                "method": method,
                "url": url,
                "data": kwargs.get("data", kwargs.get("json")),
            },
        }
        self.assertEqual(trace["graph_comment_post"]["data"], {"message": expected_text})
        print(
            "MOCKED FIRST-COMMENT TRACE " + json.dumps(trace, sort_keys=True, separators=(",", ":"))
        )

    async def test_api_draft_publishes_independent_instagram_and_facebook_comments_once(self):
        instagram_id = await self.add_account("instagram")
        facebook_id = await self.add_account("facebook")
        image_id = await self.add_public_image()
        composer, response_draft, draft = await self.save_draft(
            account_ids=[instagram_id, facebook_id],
            first_comments={
                "instagram": "A separate Instagram first comment.",
                "facebook": "A separate Facebook first comment.",
            },
            media_ids=[image_id],
            options={facebook_id: {"image": False}},
        )
        self.assertEqual(response_draft["status"], "draft")
        self.assertEqual(
            draft["firstComments"],
            {
                "instagram": "A separate Instagram first comment.",
                "facebook": "A separate Facebook first comment.",
            },
        )

        published = await self.publish_draft(composer, response_draft)
        self.assertEqual(published["status"], "published")
        await self.assert_comment_sent(response_draft["id"], instagram_id)
        await self.assert_comment_sent(response_draft["id"], facebook_id)
        calls = self.comment_calls()
        self.assertEqual(len(calls), 2)
        ig_call = next(call for call in calls if "graph.instagram.com" in call[1])
        fb_call = next(call for call in calls if "graph.facebook.com" in call[1])
        self.assertTrue(urlsplit(ig_call[1]).path.endswith(f"/{INSTAGRAM_POST_ID}/comments"))
        self.assertEqual(ig_call[2].get("data"), {"message": "A separate Instagram first comment."})
        self.assertTrue(urlsplit(fb_call[1]).path.endswith(f"/{FACEBOOK_POST_ID}/comments"))
        self.assertEqual(fb_call[2].get("data"), {"message": "A separate Facebook first comment."})
        self.assertCountEqual(
            [
                (
                    snapshot["remotePostId"],
                    snapshot["accountId"],
                    snapshot["parentStatus"],
                    snapshot["receiptStatus"],
                    snapshot["matches"],
                )
                for snapshot in self.comment_publication_snapshots
            ],
            [
                (INSTAGRAM_POST_ID, instagram_id, "published", "published", 1),
                (FACEBOOK_POST_ID, facebook_id, "published", "published", 1),
            ],
        )

        # Scheduler reconciliation may replay after restart; a sent receipt prevents a duplicate.
        await self.jobs.process_first_comments(response_draft["id"])
        self.assertEqual(len(self.comment_calls()), 2)
        self.print_trace(
            platform="instagram",
            composer=composer,
            draft=draft,
            published=published,
            account_id=instagram_id,
            expected_text="A separate Instagram first comment.",
        )
        self.print_trace(
            platform="facebook",
            composer=composer,
            draft=draft,
            published=published,
            account_id=facebook_id,
            expected_text="A separate Facebook first comment.",
        )

    async def test_processing_or_unconfirmed_publication_never_sends_first_comment(self):
        instagram_id = await self.add_account("instagram")
        image_id = await self.add_public_image()
        composer, draft_response, _ = await self.save_draft(
            account_ids=[instagram_id],
            first_comments={"instagram": "Wait until Instagram confirms."},
            media_ids=[image_id],
        )
        self.instagram_container_status = "IN_PROGRESS"
        instagram_post = await self.publish_draft(composer, draft_response)
        self.assertEqual(instagram_post["status"], "processing")
        self.assertEqual(self.comment_calls(), [])

        facebook_id = await self.add_account("facebook")
        self.facebook_confirmed = False
        fb_composer, fb_draft_response, _ = await self.save_draft(
            account_ids=[facebook_id],
            first_comments={"facebook": "Wait for Facebook confirmation."},
        )
        facebook_post = await self.publish_draft(fb_composer, fb_draft_response)
        self.assertEqual(facebook_post["status"], "needs_review")
        self.assertEqual(self.comment_calls(), [])

    async def test_empty_and_legacy_first_comments_are_skipped(self):
        facebook_id = await self.add_account("facebook")
        composer, draft_response, draft = await self.save_draft(
            account_ids=[facebook_id],
            first_comments={"instagram": "  ", "facebook": "\t"},
        )
        self.assertNotIn("firstComments", draft)
        published = await self.publish_draft(composer, draft_response)
        self.assertEqual(published["status"], "published")
        self.assertEqual(self.comment_calls(), [])

        # A pre-feature published row has receipts but no firstComments field.
        legacy_id = "legacy-first-comment-post"
        await self.store.put(
            "posts",
            {
                "id": legacy_id,
                "text": "Older post",
                "accountIds": [facebook_id],
                "mediaIds": [],
                "status": "published",
                "results": [
                    {"accountId": facebook_id, "status": "published", "remoteId": FACEBOOK_POST_ID}
                ],
            },
        )
        calls_before = len(self.social_calls())
        await self.jobs.process_first_comments(legacy_id)
        self.assertEqual(len(self.social_calls()), calls_before)
        self.assertEqual(self.comment_calls(), [])


if __name__ == "__main__":
    unittest.main()
