import asyncio
import json
import tempfile
import unittest
from collections import defaultdict, deque
from pathlib import Path
from unittest.mock import AsyncMock

from lightpost.jobs import Scheduler
from lightpost.providers import ProviderError, Providers, now, platform_error
from lightpost.security import Vault
from lightpost.store import Store


KEY = "c3" * 32
PAGE_ID = "1234567890"


class MockCommentProvider:
    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = defaultdict(deque)
        for account_id, values in (outcomes or {}).items():
            self.outcomes[account_id].extend(values)

    async def create_first_comment(self, account, remote_post_id, text):
        self.calls.append((account["id"], account["platform"], remote_post_id, text))
        outcomes = self.outcomes[account["id"]]
        outcome = outcomes.popleft() if outcomes else f"comment_{account['id']}"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def account(id, platform, remote_id):
    return {
        "id": id,
        "platform": platform,
        "remoteId": remote_id,
        "demo": False,
        "name": platform,
    }


class FirstCommentJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = await Store(":memory:").open()
        self.provider = MockCommentProvider()
        self.jobs = Scheduler(self.store, self.provider, self.directory.name)

    async def asyncTearDown(self):
        await self.jobs.close()
        await self.store.close()
        self.directory.cleanup()

    async def save_account(self, value):
        await self.store.put("accounts", value)

    async def save_post(
        self,
        post_id,
        accounts,
        comments=None,
        receipts=None,
        status="published",
    ):
        value = {
            "id": post_id,
            "status": status,
            "accountIds": [value["id"] for value in accounts],
            "results": receipts
            if receipts is not None
            else [
                {
                    "accountId": value["id"],
                    "status": "published",
                    "remoteId": (
                        value["remoteId"] + "_42"
                        if value["platform"] == "facebook"
                        else "987654321098765"
                    ),
                }
                for value in accounts
            ],
        }
        if comments is not None:
            value["firstComments"] = comments
        await self.store.put("posts", value)

    async def test_comment_waits_for_each_confirmed_receipt_and_real_remote_id(self):
        instagram = account("ig", "instagram", "ig-user")
        facebook = account("fb", "facebook", PAGE_ID)
        await self.save_account(instagram)
        await self.save_account(facebook)
        await self.save_post(
            "gated",
            [instagram, facebook],
            {"instagram": "IG opening", "facebook": "FB opening"},
            [
                {"accountId": "ig", "status": "processing", "remoteId": "987654321098765"},
                {"accountId": "fb", "status": "published", "remoteId": PAGE_ID + "_42"},
            ],
            status="partial",
        )
        await self.save_post(
            "missing-id",
            [instagram],
            {"instagram": "Do not guess the media ID"},
            [{"accountId": "ig", "status": "published", "containerId": "111222333"}],
        )

        await self.jobs.process_first_comments()

        self.assertEqual([call[0] for call in self.provider.calls], ["fb"])
        gated = {item["accountId"]: item for item in await self.jobs.first_comment_results("gated")}
        self.assertEqual(gated["fb"]["status"], "sent")
        self.assertNotIn("ig", gated)
        missing = await self.jobs.first_comment_results("missing-id")
        self.assertEqual(missing[0]["status"], "needs_review")
        self.assertNotIn("remoteCommentId", missing[0])
        self.assertEqual((await self.store.get("posts", "gated"))["status"], "partial")

    async def test_legacy_empty_and_whitespace_comments_are_noops(self):
        instagram = account("ig", "instagram", "ig-user")
        await self.save_account(instagram)
        await self.save_post("legacy", [instagram], comments=None)
        await self.save_post("empty", [instagram], comments={})
        await self.save_post("whitespace", [instagram], {"instagram": " \n\t "})

        await self.jobs.process_first_comments()

        self.assertEqual(self.provider.calls, [])
        self.assertEqual(await self.store.all("first_comments"), [])

    async def test_same_platform_accounts_and_facebook_are_independent(self):
        accounts = [
            account("ig-one", "instagram", "ig-user-1"),
            account("ig-two", "instagram", "ig-user-2"),
            account("fb", "facebook", PAGE_ID),
        ]
        for value in accounts:
            await self.save_account(value)
        await self.save_post(
            "multi",
            accounts,
            {"instagram": "Instagram comment", "facebook": "Facebook comment"},
        )

        await self.jobs.process_first_comments("multi")

        self.assertEqual(len(self.provider.calls), 3)
        self.assertEqual({call[0] for call in self.provider.calls}, {"ig-one", "ig-two", "fb"})
        self.assertEqual(
            {item["accountId"] for item in await self.jobs.first_comment_results("multi")},
            {"ig-one", "ig-two", "fb"},
        )
        self.assertTrue(
            all(item["status"] == "sent" for item in await self.jobs.first_comment_results("multi"))
        )

    async def test_successful_replay_is_idempotent_across_scheduler_restart(self):
        instagram = account("ig", "instagram", "ig-user")
        await self.save_account(instagram)
        await self.save_post("replay", [instagram], {"instagram": "Hello"})
        await self.jobs.process_first_comments("replay")
        before = await self.jobs.first_comment_results("replay")

        restarted = Scheduler(self.store, self.provider, self.directory.name)
        await restarted.recover_first_comments()
        await restarted.process_first_comments("replay")

        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(await restarted.first_comment_results("replay"), before)
        await restarted.close()

    async def test_confirmed_rejection_can_be_retried_without_changing_publish_status(self):
        instagram = account("ig", "instagram", "ig-user")
        await self.save_account(instagram)
        await self.save_post("reject", [instagram], {"instagram": "Hello"})
        self.provider.outcomes["ig"].extend(
            [
                ProviderError("Meta rejected invalid comment input.", status=400, code=100),
                "comment_ig",
            ]
        )

        await self.jobs.process_first_comments("reject")
        self.assertEqual((await self.jobs.first_comment_results("reject"))[0]["status"], "rejected")
        self.assertEqual((await self.store.get("posts", "reject"))["status"], "published")
        await self.jobs.process_first_comments("reject")
        self.assertEqual(len(self.provider.calls), 1)

        await self.jobs.retry_first_comment("reject", "ig")
        await self.jobs.process_first_comments("reject")

        result = (await self.jobs.first_comment_results("reject"))[0]
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["remoteCommentId"], "comment_ig")
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual((await self.store.get("posts", "reject"))["status"], "published")

    async def test_documented_transient_graph_codes_retry_after_scheduler_restart(self):
        instagram = account("ig", "instagram", "ig-user")
        await self.save_account(instagram)
        restarted = Scheduler(self.store, self.provider, self.directory.name)
        for code in (2, 341):
            post_id = f"transient-{code}"
            await self.save_post(post_id, [instagram], {"instagram": "Hello"})
            self.provider.outcomes["ig"].extend(
                [
                    ProviderError("Confirmed temporary Graph error.", status=400, code=code),
                    f"comment_{code}",
                ]
            )

            await self.jobs.process_first_comments(post_id)
            record = next(
                value
                for value in await self.store.all("first_comments")
                if value["postId"] == post_id
            )
            self.assertEqual(record["status"], "pending")
            self.assertTrue(record["retryAt"])
            attempts = len(self.provider.calls)

            await restarted.recover_first_comments()
            await restarted.process_first_comments(post_id)
            self.assertEqual(len(self.provider.calls), attempts)

            record["retryAt"] = "2000-01-01T00:00:00.000Z"
            await self.store.put("first_comments", record)
            await restarted.process_first_comments(post_id)
            result = (await restarted.first_comment_results(post_id))[0]
            self.assertEqual(result["status"], "sent")
            self.assertEqual(result["remoteCommentId"], f"comment_{code}")
        self.assertEqual((await self.store.get("posts", "transient-2"))["status"], "published")
        self.assertEqual((await self.store.get("posts", "transient-341"))["status"], "published")
        await restarted.close()

    async def test_file_store_preserves_retry_sent_and_interrupted_states(self):
        path = Path(self.directory.name) / "durable-comments.sqlite"
        instagram = account("ig", "instagram", "ig-user")
        self.provider.outcomes["ig"].extend(
            [ProviderError("Temporary Graph failure.", status=400, code=2), "comment_durable"]
        )
        stores = []
        schedulers = []

        async def reopen():
            store = await Store(str(path)).open()
            scheduler = Scheduler(store, self.provider, self.directory.name)
            stores.append(store)
            schedulers.append(scheduler)
            return store, scheduler

        try:
            first_store, first_jobs = await reopen()
            await first_store.put("accounts", instagram)
            await self.save_post("durable-retry", [instagram], {"instagram": "Hello"})
            post = await self.store.get("posts", "durable-retry")
            await first_store.put("posts", post)
            await first_jobs.process_first_comments("durable-retry")
            retry = next(
                value
                for value in await first_store.all("first_comments")
                if value["postId"] == "durable-retry"
            )
            self.assertEqual(retry["status"], "pending")
            self.assertTrue(retry["retryAt"])
            await first_jobs.close()
            schedulers.remove(first_jobs)
            await first_store.close()
            stores.remove(first_store)

            second_store, second_jobs = await reopen()
            await second_jobs.recover_first_comments()
            retry = next(
                value
                for value in await second_store.all("first_comments")
                if value["postId"] == "durable-retry"
            )
            self.assertTrue(retry["retryAt"])
            await second_jobs.process_first_comments("durable-retry")
            self.assertEqual(len(self.provider.calls), 1)

            retry["retryAt"] = "2000-01-01T00:00:00.000Z"
            await second_store.put("first_comments", retry)
            await second_jobs.process_first_comments("durable-retry")
            self.assertEqual(
                (await second_jobs.first_comment_results("durable-retry"))[0]["status"],
                "sent",
            )
            await second_jobs.close()
            schedulers.remove(second_jobs)
            await second_store.close()
            stores.remove(second_store)

            third_store, third_jobs = await reopen()
            await third_jobs.recover_first_comments()
            await third_jobs.process_first_comments("durable-retry")
            receipt = (await third_jobs.first_comment_results("durable-retry"))[0]
            self.assertEqual(receipt["status"], "sent")
            self.assertEqual(receipt["remoteCommentId"], "comment_durable")
            self.assertEqual(len(self.provider.calls), 2)

            await third_store.put(
                "posts",
                {
                    "id": "durable-interrupted",
                    "status": "published",
                    "accountIds": ["ig"],
                    "firstComments": {"instagram": "Again"},
                    "results": [
                        {
                            "accountId": "ig",
                            "status": "published",
                            "remoteId": "987654321098765",
                        }
                    ],
                },
            )
            await third_jobs.reconcile_first_comments("durable-interrupted")
            interrupted = next(
                value
                for value in await third_store.all("first_comments")
                if value["postId"] == "durable-interrupted"
            )
            interrupted.update(status="sending", attempts=1)
            await third_store.put("first_comments", interrupted)
            await third_jobs.close()
            schedulers.remove(third_jobs)
            await third_store.close()
            stores.remove(third_store)

            fourth_store, fourth_jobs = await reopen()
            await fourth_jobs.recover_first_comments()
            self.assertEqual(
                (await fourth_jobs.first_comment_results("durable-interrupted"))[0]["status"],
                "uncertain",
            )
            await fourth_jobs.process_first_comments("durable-interrupted")
            self.assertEqual(len(self.provider.calls), 2)
        finally:
            for scheduler in reversed(schedulers):
                await scheduler.close()
            for store in reversed(stores):
                await store.close()

    async def test_ambiguous_failure_is_persisted_and_not_automatically_retried(self):
        instagram = account("ig", "instagram", "ig-user")
        await self.save_account(instagram)
        await self.save_post("uncertain", [instagram], {"instagram": "Hello"})
        self.provider.outcomes["ig"].append(
            ProviderError("Connection interrupted.", uncertain=True)
        )

        await self.jobs.process_first_comments("uncertain")
        await self.jobs.process_first_comments("uncertain")

        self.assertEqual(
            (await self.jobs.first_comment_results("uncertain"))[0]["status"], "uncertain"
        )
        self.assertEqual(len(self.provider.calls), 1)

    async def test_rate_limit_uses_durable_backoff_then_retries_automatically(self):
        instagram = account("ig", "instagram", "ig-user")
        await self.save_account(instagram)
        await self.save_post("rate-limit", [instagram], {"instagram": "Hello"})
        self.provider.outcomes["ig"].extend(
            [ProviderError("Meta rate limit reached.", status=400, code=80002), "comment_ig"]
        )

        await self.jobs.process_first_comments("rate-limit")
        record = (await self.store.all("first_comments"))[0]
        self.assertEqual(record["status"], "pending")
        self.assertTrue(record["retryAt"])
        self.assertEqual(len(self.provider.calls), 1)

        record["retryAt"] = "2000-01-01T00:00:00.000Z"
        await self.store.put("first_comments", record)
        await self.jobs.process_first_comments("rate-limit")

        self.assertEqual((await self.jobs.first_comment_results("rate-limit"))[0]["status"], "sent")
        self.assertEqual(len(self.provider.calls), 2)

    async def test_restart_recovers_publish_before_enqueue_and_holds_interrupted_send(self):
        instagram = account("ig", "instagram", "ig-user")
        await self.save_account(instagram)
        await self.save_post("enqueue-window", [instagram], {"instagram": "Hello"})

        restarted = Scheduler(self.store, self.provider, self.directory.name)
        await restarted.recover_first_comments()
        self.assertEqual((await self.store.all("first_comments"))[0]["status"], "pending")
        await restarted.process_first_comments("enqueue-window")
        self.assertEqual(len(self.provider.calls), 1)

        await self.save_post("interrupted", [instagram], {"instagram": "Again"})
        await restarted.reconcile_first_comments("interrupted")
        record = next(
            item
            for item in await self.store.all("first_comments")
            if item["postId"] == "interrupted"
        )
        record.update(status="sending", attempts=1)
        await self.store.put("first_comments", record)
        recovered = Scheduler(self.store, self.provider, self.directory.name)
        await recovered.recover_first_comments()
        self.assertEqual(
            (await recovered.first_comment_results("interrupted"))[0]["status"], "uncertain"
        )
        await recovered.process_first_comments("interrupted")
        self.assertEqual(len(self.provider.calls), 1)
        await restarted.close()
        await recovered.close()

    async def test_comment_errors_appear_in_attention_without_changing_publish_status(self):
        instagram = account("ig", "instagram", "ig-user")
        await self.save_account(instagram)
        for post_id, status in (
            ("published-rejected", "rejected"),
            ("published-uncertain", "uncertain"),
            ("published-pending", "pending"),
        ):
            await self.save_post(post_id, [instagram])
            await self.store.put(
                "first_comments",
                {
                    "id": "job-" + post_id,
                    "postId": post_id,
                    "accountId": "ig",
                    "platform": "instagram",
                    "status": status,
                },
            )

        first_page, total = await self.store.posts("attention", offset=0, limit=1)
        second_page, second_total = await self.store.posts("attention", offset=1, limit=1)

        self.assertEqual(total, 2)
        self.assertEqual(second_total, 2)
        self.assertEqual(
            {first_page[0]["id"], second_page[0]["id"]},
            {"published-rejected", "published-uncertain"},
        )
        self.assertEqual(
            (await self.store.get("posts", "published-rejected"))["status"], "published"
        )

    async def test_concurrent_scheduler_calls_only_post_once_per_account(self):
        instagram = account("ig", "instagram", "ig-user")
        await self.save_account(instagram)
        await self.save_post("concurrent", [instagram], {"instagram": "Hello"})
        entered = asyncio.Event()
        release = asyncio.Event()

        async def create_comment(*_):
            self.provider.calls.append(("ig", "instagram", "987654321098765", "Hello"))
            entered.set()
            await release.wait()
            return "comment_ig"

        self.provider.create_first_comment = create_comment
        first = asyncio.create_task(self.jobs.process_first_comments("concurrent"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(self.jobs.process_first_comments("concurrent"))
        release.set()
        await asyncio.gather(first, second)

        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual((await self.jobs.first_comment_results("concurrent"))[0]["status"], "sent")

    async def test_stalled_comment_does_not_block_another_platform_scheduler_tick(self):
        instagram = account("ig", "instagram", "ig-user")
        bluesky = account("bsky", "bluesky", "did:plc:mock")
        await self.save_account(instagram)
        await self.save_account(bluesky)
        await self.save_post("comment-post", [instagram], {"instagram": "Hello"})
        await self.store.put(
            "posts",
            {
                "id": "due-bluesky",
                "text": "Publish while the comment request is stalled.",
                "accountIds": ["bsky"],
                "mediaIds": [],
                "status": "queued",
                "scheduledAt": None,
                "createdAt": now(),
                "results": [],
            },
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def stalled_comment(*_):
            self.provider.calls.append(("ig", "instagram", "987654321098765", "Hello"))
            started.set()
            await release.wait()
            return "comment_ig"

        self.provider.create_first_comment = stalled_comment
        self.provider.publish = AsyncMock(
            return_value={"status": "published", "remoteId": "at://mock/post"}
        )
        try:
            await asyncio.wait_for(self.jobs.tick(), timeout=1)
            await asyncio.wait_for(started.wait(), timeout=1)
            other_post = await self.store.get("posts", "due-bluesky")
            self.assertEqual(other_post["status"], "published")
            self.assertEqual(self.provider.publish.await_count, 1)
        finally:
            release.set()

        await asyncio.wait_for(self.jobs.wait_for_first_comments("comment-post"), timeout=1)
        self.assertEqual(
            (await self.jobs.first_comment_results("comment-post"))[0]["status"], "sent"
        )

    async def test_comment_backlog_does_not_hold_unstarted_destination_lock(self):
        accounts = [account(f"fb-{index}", "facebook", f"{PAGE_ID}{index}") for index in range(4)]
        for value in accounts:
            await self.save_account(value)
        for index, value in enumerate(accounts):
            await self.save_post(f"published-{index}", [value], {"facebook": f"Comment {index}"})

        started = asyncio.Queue()
        release = asyncio.Event()

        async def stalled_comment(value, remote_post_id, comment_text):
            self.provider.calls.append(
                (value["id"], value["platform"], remote_post_id, comment_text)
            )
            await started.put(value["id"])
            await release.wait()
            return f"comment_{value['id']}"

        self.provider.create_first_comment = stalled_comment
        self.provider.publish = AsyncMock(
            return_value={"status": "published", "remoteId": "ig-new-post"}
        )
        self.jobs.schedule_first_comments()
        try:
            first_three = await asyncio.wait_for(
                asyncio.gather(*(started.get() for _ in range(3))), timeout=1
            )
            self.assertEqual(len(set(first_three)), 3)
            await self.store.put(
                "posts",
                {
                    "id": "due-on-fourth-account",
                    "text": "This due publication must not wait for a comment slot.",
                    "accountIds": [accounts[3]["id"]],
                    "mediaIds": [],
                    "status": "queued",
                    "scheduledAt": None,
                    "createdAt": now(),
                    "results": [],
                },
            )

            await asyncio.wait_for(self.jobs.tick(), timeout=1)
            published = await self.store.get("posts", "due-on-fourth-account")
            self.assertEqual(published["status"], "published")
            self.assertEqual(self.provider.publish.await_count, 1)
        finally:
            release.set()

        await asyncio.wait_for(self.jobs.wait_for_first_comments(), timeout=1)
        self.assertEqual(len(self.provider.calls), 4)


class FirstCommentProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.vault = Vault(KEY)
        self.send = AsyncMock()
        self.providers = Providers(self.vault, self.send)

    async def test_instagram_uses_instagram_graph_media_comments_edge(self):
        self.send.return_value = {"id": "987654321098765"}
        instagram = {
            "id": "ig",
            "platform": "instagram",
            "remoteId": "123456789",
            "credentials": self.vault.encrypt({"token": "MOCK_IG_TOKEN"}),
        }

        result = await self.providers.create_first_comment(
            instagram, "987654321098765", "Opening note"
        )

        self.assertEqual(result, "987654321098765")
        call = self.send.call_args
        self.assertEqual(call.args[0], "POST")
        self.assertIn("https://graph.instagram.com/", call.args[1])
        self.assertTrue(call.args[1].endswith("/987654321098765/comments"))
        self.assertEqual(call.kwargs["data"], {"message": "Opening note"})
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer MOCK_IG_TOKEN")

    async def test_facebook_uses_page_post_comments_edge(self):
        self.send.return_value = {"id": PAGE_ID + "_42_99"}
        facebook = {
            "id": "fb",
            "platform": "facebook",
            "remoteId": PAGE_ID,
            "credentials": self.vault.encrypt({"token": "MOCK_FB_TOKEN", "pageId": PAGE_ID}),
        }

        result = await self.providers.create_first_comment(facebook, PAGE_ID + "_42", "Thanks")

        self.assertEqual(result, PAGE_ID + "_42_99")
        call = self.send.call_args
        self.assertEqual(call.args[0], "POST")
        self.assertTrue(call.args[1].endswith("/" + PAGE_ID + "_42/comments"))
        self.assertEqual(call.kwargs["data"], {"message": "Thanks"})
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer MOCK_FB_TOKEN")

    async def test_container_id_and_malformed_success_receipt_are_never_used_as_comment_targets(
        self,
    ):
        instagram = {
            "id": "ig",
            "platform": "instagram",
            "remoteId": "123456789",
            "credentials": self.vault.encrypt({"token": "MOCK_IG_TOKEN"}),
        }
        with self.assertRaises(ValueError):
            await self.providers.create_first_comment(instagram, "container-id", "Hello")
        self.send.assert_not_awaited()

        self.send.return_value = {"id": {"unexpected": "shape"}}
        with self.assertRaises(ProviderError) as caught:
            await self.providers.create_first_comment(instagram, "987654321098765", "Hello")
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.send.await_count, 1)

    async def test_unknown_429_and_ambiguous_http_responses_are_held_for_review(self):
        instagram = {
            "id": "ig",
            "platform": "instagram",
            "remoteId": "123456789",
            "credentials": self.vault.encrypt({"token": "MOCK_IG_TOKEN"}),
        }
        for error in (
            ProviderError("Unrecognized 429 response.", status=429),
            ProviderError("Request timed out.", status=408),
            ProviderError("Unexpected redirect.", status=302),
            ProviderError("Unknown Graph rejection.", status=400),
            ProviderError("API Unknown.", status=400, code=1),
        ):
            with self.subTest(status=error.status, code=error.code):
                self.send.reset_mock()
                self.send.side_effect = error
                with self.assertRaises(ProviderError) as caught:
                    await self.providers.create_first_comment(instagram, "987654321098765", "Hello")
                self.assertTrue(caught.exception.uncertain)

        self.send.reset_mock()
        self.send.side_effect = ProviderError("Documented rate limit.", status=400, code=80002)
        with self.assertRaises(ProviderError) as caught:
            await self.providers.create_first_comment(instagram, "987654321098765", "Hello")
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.code, 80002)

        self.send.reset_mock()
        self.send.side_effect = ProviderError("Temporary Graph service error.", status=400, code=2)
        with self.assertRaises(ProviderError) as caught:
            await self.providers.create_first_comment(instagram, "987654321098765", "Hello")
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.code, 2)

        self.send.reset_mock()
        self.send.side_effect = ProviderError("Invalid comment parameters.", status=400, code=100)
        with self.assertRaises(ProviderError) as caught:
            await self.providers.create_first_comment(instagram, "987654321098765", "Hello")
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.code, 100)

    def test_meta_graph_error_codes_keep_retry_and_permanent_classes_distinct(self):
        for code in (2, 4, 17, 32, 341, 613, 80001, 80002):
            with self.subTest(code=code):
                error = platform_error(
                    400,
                    "graph.instagram.com",
                    json.dumps({"error": {"code": code, "message": "mocked"}}).encode(),
                )
                classified = self.providers.first_comment_provider_error(error, "instagram")
                self.assertFalse(classified.uncertain)
                self.assertEqual(classified.code, code)

        unknown = platform_error(
            400,
            "graph.instagram.com",
            json.dumps({"error": {"code": 1, "message": "mocked"}}).encode(),
        )
        self.assertTrue(self.providers.first_comment_provider_error(unknown, "instagram").uncertain)
        permanent = platform_error(
            400,
            "graph.instagram.com",
            json.dumps({"error": {"code": 100, "message": "mocked"}}).encode(),
        )
        self.assertFalse(
            self.providers.first_comment_provider_error(permanent, "instagram").uncertain
        )


if __name__ == "__main__":
    unittest.main()
