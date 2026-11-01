import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from lightpost.app import KEYS, create_app
from lightpost.config import Config


class FirstCommentApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        config = Config(
            database=str(Path(self.directory.name) / "first-comments.sqlite"),
            key="b7" * 32,
            scheduler=False,
        )
        self.client = TestClient(TestServer(create_app(config)))
        await self.client.start_server()
        app = self.client.server.app
        self.store = app[KEYS["store"]]
        self.jobs = app[KEYS["jobs"]]
        for identity, platform in (("ig", "instagram"), ("fb", "facebook")):
            await self.store.put(
                "accounts",
                {
                    "id": identity,
                    "platform": platform,
                    "name": platform,
                    "handle": "@test",
                    "remoteId": "100000001" if platform == "instagram" else "100000002",
                    "demo": False,
                },
            )

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def create_post(self, **changes):
        return await self.client.post(
            "/api/posts",
            json={
                "text": "A post",
                "accountIds": ["ig", "fb"],
                "mediaIds": [],
                "status": "draft",
                **changes,
            },
        )

    async def test_first_comments_normalize_and_round_trip_on_draft(self):
        response = await self.create_post(
            firstComments={"instagram": "  Instagram copy  ", "facebook": " Facebook copy "}
        )
        self.assertEqual(response.status, 201)
        saved = await response.json()
        self.assertEqual(
            saved["firstComments"],
            {"instagram": "Instagram copy", "facebook": "Facebook copy"},
        )
        listed = await self.client.get("/api/posts")
        post = (await listed.json())["posts"][0]
        self.assertEqual(post["firstComments"], saved["firstComments"])
        self.assertEqual(post["firstCommentResults"], [])

    async def test_omitted_update_preserves_and_explicit_empty_clears(self):
        response = await self.create_post(firstComments={"instagram": "Keep this"})
        self.assertEqual(response.status, 201)
        post = await response.json()

        response = await self.create_post(id=post["id"])
        self.assertEqual(response.status, 201)
        self.assertEqual((await response.json())["firstComments"], {"instagram": "Keep this"})

        response = await self.create_post(id=post["id"], firstComments={})
        self.assertEqual(response.status, 201)
        cleared = await response.json()
        self.assertNotIn("firstComments", cleared)

    async def test_whitespace_only_map_clears_and_legacy_posts_remain_legacy(self):
        response = await self.create_post(firstComments={"facebook": "Page comment"})
        post = await response.json()
        response = await self.create_post(id=post["id"], firstComments={"facebook": " \n\t "})
        self.assertEqual(response.status, 201)
        self.assertNotIn("firstComments", await response.json())

        legacy = await self.create_post()
        legacy_post = await legacy.json()
        self.assertNotIn("firstComments", legacy_post)
        response = await self.create_post(id=legacy_post["id"])
        self.assertEqual(response.status, 201)
        self.assertNotIn("firstComments", await response.json())

    async def test_rejects_malformed_first_comment_maps(self):
        cases = (
            ([], "Choose valid Instagram and Facebook first comments."),
            ({"threads": "not supported"}, "Choose valid Instagram and Facebook first comments."),
            ({"instagram": 10}, "First comments must be text."),
            ({"facebook": "x" * 10001}, "10,000 characters"),
        )
        for value, expected in cases:
            with self.subTest(value=str(value)[:40]):
                response = await self.create_post(firstComments=value)
                self.assertEqual(response.status, 400)
                self.assertIn(expected, (await response.json())["error"])

    async def test_retry_route_queues_only_the_target_comment_without_changing_post(self):
        response = await self.create_post(firstComments={"instagram": "Comment text"})
        post = await response.json()
        post.update(
            status="published",
            results=[
                {"accountId": "ig", "status": "published", "remoteId": "800000001"},
                {"accountId": "fb", "status": "published", "remoteId": "100000002_800000002"},
            ],
        )
        await self.store.put("posts", post)
        record_id = self.jobs.first_comment_record_id(post["id"], "ig")
        await self.store.put(
            "first_comments",
            {
                "id": record_id,
                "postId": post["id"],
                "accountId": "ig",
                "platform": "instagram",
                "remotePostId": "800000001",
                "text": "Comment text",
                "status": "rejected",
                "attempts": 1,
                "updatedAt": "2026-11-01T00:00:00Z",
                "message": "Meta rejected the comment.",
            },
        )

        response = await self.client.post(
            f"/api/posts/{post['id']}/first-comments/ig/retry", json={}
        )
        self.assertEqual(response.status, 202, await response.text())
        record = await self.store.get("first_comments", record_id)
        self.assertEqual(record["status"], "pending")
        self.assertEqual((await self.store.get("posts", post["id"]))["status"], "published")
        self.assertTrue(self.jobs.wake.is_set())

    async def test_comment_statuses_are_separate_in_state_and_post_list(self):
        response = await self.create_post(firstComments={"instagram": "Comment text"})
        post = await response.json()
        result = {
            "accountId": "ig",
            "platform": "instagram",
            "status": "needs_review",
            "message": "Check the destination before continuing.",
            "updatedAt": "2026-11-01T00:00:00Z",
        }
        await self.store.put(
            "first_comments",
            {
                "id": self.jobs.first_comment_record_id(post["id"], "ig"),
                "postId": post["id"],
                **result,
                "text": "Comment text",
                "remotePostId": "800000001",
            },
        )

        listed = await self.client.get("/api/posts")
        self.assertEqual((await listed.json())["posts"][0]["firstCommentResults"], [result])
        state = await self.client.get("/api/state")
        state_post = next(
            item for item in (await state.json())["posts"] if item["id"] == post["id"]
        )
        self.assertEqual(state_post["firstCommentResults"], [result])


if __name__ == "__main__":
    unittest.main()
