import json
import time
import unittest
from unittest.mock import AsyncMock

from lightpost.providers import (
    Providers,
    ProviderError,
    discord_webhook,
    destination_post,
    image_url,
    now,
    platform_error,
    validate_post,
)
from lightpost.security import Vault
import test_python as original
from test_python import KEY, PNG, post

WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/" + "test_only_webhook_token_12345"
JPEG = b"\xff\xd8\xff" + b"test-image"


class NewProviderTests(unittest.IsolatedAsyncioTestCase):
    def providers(self, responses):
        send = AsyncMock(side_effect=responses)
        return Providers(Vault(KEY), send, pause=AsyncMock()), send

    def meta_account(self, platform="threads", **credentials):
        return {
            "id": "meta",
            "name": platform.title(),
            "platform": platform,
            "remoteId": "12345",
            "credentials": Vault(KEY).encrypt(
                {"token": "test-only-meta-token", "userId": "12345", **credentials}
            ),
        }

    def test_mandatory_content_cannot_be_disabled(self):
        value = {
            "text": "Hello",
            "mediaIds": ["image"],
            "contentOptions": {"a": {"text": False, "image": False}},
        }
        instagram = destination_post(
            {"id": "a", "platform": "instagram", "name": "Instagram"}, value
        )
        self.assertEqual(instagram["mediaIds"], ["image"])
        self.assertEqual(instagram["text"], "")
        x = destination_post({"id": "a", "platform": "x", "name": "X"}, value)
        self.assertEqual(x["text"], "Hello")
        self.assertEqual(x["mediaIds"], [])
        validate_post({"id": "a", "platform": "x", "name": "X"}, x)
        validate_post({"id": "a", "platform": "instagram", "name": "Instagram"}, instagram)

    async def test_x_verification_rejection_identifies_oauth_credentials_without_secrets(self):
        for status in (401, 403):
            providers, send = self.providers([ProviderError("rejected", status=status)])
            with self.assertRaises(ProviderError) as caught:
                await providers.connect(
                    {
                        "platform": "x",
                        "secret": "private-user-token",
                        "apiKey": "private-consumer-key",
                        "apiSecret": "private-consumer-secret",
                        "tokenSecret": "private-user-secret",
                    }
                )
            message = str(caught.exception)
            self.assertIn(f"HTTP {status}", message)
            self.assertIn("Consumer Secret", message)
            self.assertNotIn("private-", message)
            self.assertEqual(send.await_count, 1)

    async def test_x_publishing_permission_error_is_actionable_and_does_not_retry(self):
        providers, send = self.providers([ProviderError("rejected", status=403)])
        account = {
            "id": "x",
            "name": "X",
            "platform": "x",
            "credentials": Vault(KEY).encrypt(
                {
                    "apiKey": "test-only",
                    "apiSecret": "test-only",
                    "token": "test-only",
                    "tokenSecret": "test-only",
                }
            ),
        }
        with self.assertRaises(ProviderError) as caught:
            await providers.publish(account, post(), [], AsyncMock())
        self.assertIn("Read and Write", str(caught.exception))
        self.assertIn("regenerate", str(caught.exception))
        self.assertNotIn("test-only", str(caught.exception))
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(send.await_count, 1)

    async def test_discord_connect_verifies_incoming_webhook_without_posting(self):
        providers, send = self.providers(
            [
                {
                    "id": "123456789012345678",
                    "type": 1,
                    "guild_id": "98765",
                    "channel_id": "56789",
                    "name": "updates",
                }
            ]
        )
        account = await providers.connect({"platform": "discord", "secret": WEBHOOK})
        self.assertEqual(account["handle"], "#updates")
        self.assertNotIn(
            "test_only_webhook_token",
            json.dumps({k: v for k, v in account.items() if k != "credentials"}),
        )
        self.assertEqual(send.call_args.args[0], "GET")
        self.assertEqual(Vault(KEY).decrypt(account["credentials"])["channelId"], "56789")

    async def test_discord_send_waits_for_receipt_and_disables_mentions(self):
        providers, send = self.providers(
            [
                {
                    "id": "123456789012345678",
                    "type": 1,
                    "guild_id": "98765",
                    "channel_id": "56789",
                    "name": "updates",
                },
                {"id": "99999", "channel_id": "56789"},
            ]
        )
        account = {**await providers.connect({"platform": "discord", "secret": WEBHOOK}), "id": "d"}
        result = await providers.publish(
            account, post(text="Hello @everyone", deliveryAt=now()), [], AsyncMock()
        )
        self.assertEqual(result["url"], "https://discord.com/channels/98765/56789/99999")
        self.assertEqual(send.call_args.kwargs["params"]["wait"], "true")
        self.assertEqual(send.call_args.kwargs["json"]["allowed_mentions"], {"parse": []})
        payload = send.call_args.kwargs["json"]
        self.assertEqual(send.call_args.kwargs["params"]["with_components"], "true")
        self.assertEqual(payload["flags"], 32768)
        self.assertNotIn("content", payload)
        self.assertNotIn("embeds", payload)
        self.assertEqual(
            payload["components"],
            [{"type": 17, "components": [{"type": 10, "content": "Hello @everyone"}]}],
        )

    async def test_discord_public_url_gallery_and_thread_destination(self):
        providers, send = self.providers(
            [
                {"id": "123456789012345678", "type": 1, "guild_id": "98765", "channel_id": "56789"},
                {"id": "99999", "channel_id": "77777"},
            ]
        )
        account = {
            **await providers.connect(
                {"platform": "discord", "secret": WEBHOOK + "?thread_id=77777"}
            ),
            "id": "d",
        }
        await providers.publish(
            account,
            post(deliveryAt=now()),
            [
                {
                    "bytes": PNG,
                    "name": "image.png",
                    "mime": "image/png",
                    "alt": "A dot",
                    "sourceUrl": "https://example.org/image.png",
                }
            ],
            AsyncMock(),
        )
        self.assertEqual(send.call_args.kwargs["params"]["thread_id"], "77777")
        self.assertNotIn("data", send.call_args.kwargs)
        payload = send.call_args.kwargs["json"]
        self.assertNotIn("attachments", payload)
        self.assertEqual(payload["flags"], 32768)
        self.assertNotIn("content", payload)
        children = payload["components"][0]["components"]
        self.assertEqual(children[0], {"type": 10, "content": "A real idea."})
        self.assertEqual(
            children[1],
            {
                "type": 12,
                "items": [
                    {"media": {"url": "https://example.org/image.png"}, "description": "A dot"}
                ],
            },
        )
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    async def test_discord_image_only_gallery_has_no_empty_text_component(self):
        providers, send = self.providers([{"id": "99999", "channel_id": "56789"}])
        account = {
            "id": "d",
            "name": "Discord",
            "platform": "discord",
            "credentials": Vault(KEY).encrypt(
                {**discord_webhook(WEBHOOK), "guildId": "98765", "channelId": "56789"}
            ),
        }
        images = [
            {
                "bytes": PNG,
                "mime": "image/png",
                "alt": f"Image {i}",
                "sourceUrl": f"https://example.org/image-{i}.png",
            }
            for i in range(4)
        ]
        await providers.publish(
            account, post(text="", mediaIds=[str(i) for i in range(4)]), images, AsyncMock()
        )
        payload = send.call_args.kwargs["json"]
        children = payload["components"][0]["components"]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["type"], 12)
        self.assertEqual(
            [item["media"]["url"] for item in children[0]["items"]],
            [f"https://example.org/image-{i}.png" for i in range(4)],
        )
        self.assertNotIn("data", send.call_args.kwargs)
        self.assertNotIn("attachments", payload)

    async def test_discord_omits_empty_gallery_description(self):
        providers, send = self.providers([{"id": "99999", "channel_id": "56789"}])
        account = {
            "id": "d",
            "name": "Discord",
            "platform": "discord",
            "credentials": Vault(KEY).encrypt(
                {**discord_webhook(WEBHOOK), "guildId": "98765", "channelId": "56789"}
            ),
        }
        image = {
            "bytes": JPEG,
            "mime": "image/jpeg",
            "alt": "",
            "sourceUrl": "https://example.org/photo.jpg",
        }
        await providers.publish(
            account, post(text="test", mediaIds=["image"]), [image], AsyncMock()
        )
        payload = send.call_args.kwargs["json"]
        self.assertEqual(
            payload["components"][0]["components"][1]["items"],
            [{"media": {"url": "https://example.org/photo.jpg"}}],
        )
        self.assertNotIn("attachments", payload)
        self.assertEqual(
            send.call_args.kwargs["params"], {"wait": "true", "with_components": "true"}
        )

    def test_discord_validation_errors_retain_field_path_without_sensitive_text(self):
        nested = {"_errors": [{"code": "BASE_TYPE_BAD_LENGTH", "message": "private-token-and-url"}]}
        for key in reversed(["components", "0", "components", "1", "items", "0", "description"]):
            nested = {key: nested}
        nested["private-token-and-url"] = {"_errors": [{"code": "BASE_TYPE_REQUIRED"}]}
        body = json.dumps(
            {"code": 50035, "message": "private-token-and-url", "errors": nested}
        ).encode()

        error = platform_error(400, "discord.com", body)
        self.assertEqual(error.code, 50035)
        self.assertIn("components.0.components.1.items.0.description", str(error))
        self.assertIn("BASE_TYPE_BAD_LENGTH", str(error))
        self.assertNotIn("private-token-and-url", str(error))
        self.assertFalse(error.uncertain)

    async def test_discord_local_only_image_rejected_before_sending(self):
        providers, send = self.providers([])
        account = {
            "id": "d",
            "name": "Discord",
            "platform": "discord",
            "credentials": Vault(KEY).encrypt(
                {**discord_webhook(WEBHOOK), "guildId": "98765", "channelId": "56789"}
            ),
        }
        with self.assertRaisesRegex(ValueError, "public image URL"):
            await providers.publish(
                account,
                post(mediaIds=["image"]),
                [{"bytes": PNG, "mime": "image/png"}],
                AsyncMock(),
            )
        send.assert_not_awaited()

    async def test_discord_invalid_url_never_sends(self):
        providers, send = self.providers([])
        for url in [
            WEBHOOK.replace("discord.com", "discord.com.evil.example"),
            WEBHOOK.replace("https:", "http:"),
            "https://127.0.0.1/api/webhooks/12345/token",
            WEBHOOK + "/github",
            WEBHOOK + "?redirect=https://evil.example",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                await providers.connect({"platform": "discord", "secret": url})
        send.assert_not_called()
        self.assertEqual(
            discord_webhook(WEBHOOK.replace("discord.com", "discordapp.com"))["webhook"],
            WEBHOOK.replace("/api/", "/api/v10/"),
        )

    async def test_discord_without_receipt_is_uncertain(self):
        providers, _ = self.providers([{}])
        account = {
            "id": "d",
            "name": "Discord",
            "platform": "discord",
            "credentials": Vault(KEY).encrypt(
                {**discord_webhook(WEBHOOK), "guildId": "98765", "channelId": "56789"}
            ),
        }
        with self.assertRaises(ProviderError) as error:
            await providers.publish(account, post(deliveryAt=now()), [], AsyncMock())
        self.assertTrue(error.exception.uncertain)

    async def test_meta_connections_choose_correct_platform_identity(self):
        providers, send = self.providers(
            [
                {"id": "999", "user_id": "12345", "username": "creator"},
                {"id": "22222", "username": "writer"},
            ]
        )
        instagram = await providers.connect({"platform": "instagram", "secret": "test-only-token"})
        threads = await providers.connect({"platform": "threads", "secret": "test-only-token"})
        self.assertEqual(instagram["remoteId"], "12345")
        self.assertEqual(threads["remoteId"], "22222")
        self.assertIn("graph.instagram.com", send.call_args_list[0].args[1])
        self.assertIn("graph.threads.net", send.call_args_list[1].args[1])

    async def test_instagram_requires_image_and_rejects_text_only(self):
        with self.assertRaisesRegex(ValueError, "Instagram needs one public JPEG"):
            validate_post(self.meta_account("instagram"), post())

    async def test_threads_text_container_and_publication_receipt(self):
        providers, send = self.providers(
            [
                {"id": "11111"},
                {"status": "FINISHED"},
                {"id": "22222"},
                {"permalink": "https://www.threads.net/@writer/post/example"},
            ]
        )
        checkpoint = AsyncMock()
        result = await providers.publish(
            self.meta_account(), post(deliveryAt=now()), [], AsyncMock(), None, checkpoint
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(
            send.call_args_list[0].kwargs["data"], {"media_type": "TEXT", "text": "A real idea."}
        )
        self.assertTrue(send.call_args_list[2].args[1].endswith("/12345/threads_publish"))
        self.assertEqual(checkpoint.call_args.kwargs, {})
        self.assertEqual(checkpoint.call_args.args[0]["stage"], "publishing")

    async def test_instagram_container_polls_and_reuses_id(self):
        providers, send = self.providers(
            [
                {"id": "11111"},
                {"status_code": "IN_PROGRESS"},
                {"status_code": "FINISHED"},
                {"id": "22222"},
                {"permalink": "https://www.instagram.com/p/example/"},
            ]
        )
        image = {
            "bytes": JPEG,
            "mime": "image/jpeg",
            "name": "image.jpg",
            "sourceUrl": "https://example.org/image.jpg",
            "alt": "A photo",
        }
        draft = post(deliveryAt=now(), mediaIds=["image"])
        pending = await providers.publish(
            self.meta_account("instagram"), draft, [image], AsyncMock(), None, AsyncMock()
        )
        self.assertEqual(pending["status"], "processing")
        finished = await providers.publish(
            self.meta_account("instagram"), draft, [image], AsyncMock(), pending, AsyncMock()
        )
        self.assertEqual(finished["status"], "published")
        creations = [
            call
            for call in send.call_args_list
            if call.args[0] == "POST" and call.args[1].endswith("/media")
        ]
        self.assertEqual(len(creations), 1)
        self.assertEqual(creations[0].kwargs["data"]["image_url"], image["sourceUrl"])

    def test_meta_error_keeps_only_numeric_codes(self):
        body = json.dumps(
            {
                "error": {
                    "code": 9007,
                    "error_subcode": 2207027,
                    "message": "private-token https://private.example/image.jpg",
                }
            }
        ).encode()
        error = platform_error(400, "graph.instagram.com", body)
        self.assertEqual((error.code, error.subcode), (9007, 2207027))
        self.assertNotIn("private", str(error))
        for invalid in (b"not json", b"[]", b'{"error":{"code":"private-token"}}'):
            self.assertEqual(platform_error(400, "graph.instagram.com", invalid).code, 0)
        self.assertEqual(platform_error(400, "other.example", body).code, 0)

    async def test_instagram_not_ready_waits_then_reuses_same_container(self):
        providers, send = self.providers(
            [
                {"id": "11111"},
                {"status_code": "FINISHED"},
                ProviderError("Media not ready", status=400, code=9007, subcode=2207027),
                {"status_code": "FINISHED"},
                {"status_code": "FINISHED"},
                {"id": "22222"},
                {"permalink": "https://www.instagram.com/p/example/"},
            ]
        )
        image = {
            "bytes": JPEG,
            "mime": "image/jpeg",
            "name": "image.jpg",
            "sourceUrl": "https://example.org/image.jpg",
        }
        draft = post(deliveryAt=now(), mediaIds=["image"])
        account = self.meta_account("instagram")
        pending = await providers.publish(account, draft, [image], AsyncMock(), None, AsyncMock())
        self.assertEqual(pending["status"], "failed")
        self.assertEqual(pending["containerId"], "11111")
        result = await providers.publish(account, draft, [image], AsyncMock(), pending, AsyncMock())
        self.assertEqual(result["status"], "published")
        creations = [call for call in send.call_args_list if call.args[1].endswith("/media")]
        self.assertEqual(len(creations), 1)

    async def test_instagram_publish_error_then_published_is_success(self):
        for error in (ProviderError("rejected", status=400), ProviderError("lost", uncertain=True)):
            providers, send = self.providers(
                [
                    {"status_code": "FINISHED"},
                    error,
                    {"status_code": "PUBLISHED"},
                ]
            )
            image = {
                "bytes": JPEG,
                "mime": "image/jpeg",
                "name": "image.jpg",
                "sourceUrl": "https://example.org/image.jpg",
            }
            result = await providers.publish(
                self.meta_account("instagram"),
                post(deliveryAt=now(), mediaIds=["image"]),
                [image],
                AsyncMock(),
                {"containerId": "11111"},
                AsyncMock(),
            )
            self.assertEqual(result["status"], "published")
            self.assertEqual([call.args[0] for call in send.call_args_list], ["GET", "POST", "GET"])
            self.assertEqual(providers.pause.await_count, 2)

    async def test_instagram_unconfirmed_publish_only_polls_then_needs_review(self):
        providers, send = self.providers(
            [
                {"status_code": "FINISHED"},
                ProviderError("rejected", status=400, code=100),
                {"status_code": "FINISHED"},
                {"status_code": "PUBLISHED"},
            ]
        )
        image = {
            "bytes": JPEG,
            "mime": "image/jpeg",
            "name": "image.jpg",
            "sourceUrl": "https://example.org/image.jpg",
        }
        args = (
            self.meta_account("instagram"),
            post(deliveryAt=now(), mediaIds=["image"]),
            [image],
            AsyncMock(),
        )
        pending = await providers.publish(*args, {"containerId": "11111"}, AsyncMock())
        self.assertEqual(pending["status"], "uncertain")
        self.assertEqual(pending["stage"], "verifying")
        self.assertEqual(pending["providerCode"], 100)
        result = await providers.publish(*args, pending, AsyncMock())
        self.assertEqual(result["status"], "published")
        self.assertEqual(sum(call.args[0] == "POST" for call in send.call_args_list), 1)
        send.reset_mock(side_effect=True)
        send.return_value = {"status_code": "FINISHED"}
        result = await providers.publish(
            *args, {**pending, "verificationStartedAt": time.time() - 61}, AsyncMock()
        )
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(send.call_args.args[0], "GET")

    async def test_explicit_retry_can_publish_ready_container_once(self):
        providers, send = self.providers(
            [
                {"status_code": "FINISHED"},
                {"status_code": "FINISHED"},
                ProviderError("unclear", status=400),
                {"status_code": "FINISHED"},
                {"status_code": "PUBLISHED"},
            ]
        )
        image = {
            "bytes": JPEG,
            "mime": "image/jpeg",
            "name": "image.jpg",
            "sourceUrl": "https://example.org/image.jpg",
        }
        draft = post(deliveryAt=now(), mediaIds=["image"], retryRequestedAt=time.time())
        previous = {
            "containerId": "11111",
            "stage": "verifying",
            "verificationStartedAt": time.time() - 30,
        }
        args = (self.meta_account("instagram"), draft, [image], AsyncMock())
        pending = await providers.publish(*args, previous, AsyncMock())
        self.assertEqual(pending["status"], "uncertain")
        self.assertEqual(pending["retryRequestHandledAt"], draft["retryRequestedAt"])
        self.assertEqual(pending["stage"], "verifying")
        result = await providers.publish(*args, pending, AsyncMock())
        self.assertEqual(result["status"], "published")
        self.assertEqual(sum(call.args[0] == "POST" for call in send.call_args_list), 1)
        self.assertTrue(all(not call.args[1].endswith("/media") for call in send.call_args_list))

    async def test_explicit_retry_never_resends_published_container(self):
        providers, send = self.providers([{"status_code": "PUBLISHED"}])
        image = {
            "bytes": JPEG,
            "mime": "image/jpeg",
            "name": "image.jpg",
            "sourceUrl": "https://example.org/image.jpg",
        }
        result = await providers.publish(
            self.meta_account("instagram"),
            post(deliveryAt=now(), mediaIds=["image"], retryRequestedAt=time.time()),
            [image],
            AsyncMock(),
            {"containerId": "11111", "stage": "verifying"},
            AsyncMock(),
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(send.await_count, 1)
        self.assertEqual(send.call_args.args[0], "GET")

    async def test_instagram_missing_publish_receipt_is_verified(self):
        providers, send = self.providers(
            [{"status_code": "FINISHED"}, {}, {"status_code": "PUBLISHED"}]
        )
        image = {
            "bytes": JPEG,
            "mime": "image/jpeg",
            "name": "image.jpg",
            "sourceUrl": "https://example.org/image.jpg",
        }
        result = await providers.publish(
            self.meta_account("instagram"),
            post(deliveryAt=now(), mediaIds=["image"]),
            [image],
            AsyncMock(),
            {"containerId": "11111"},
            AsyncMock(),
        )
        self.assertEqual(result["status"], "published")

    async def test_meta_existing_published_container_never_republishes(self):
        providers, send = self.providers([{"status": "PUBLISHED"}])
        result = await providers.publish(
            self.meta_account(),
            post(deliveryAt=now()),
            [],
            AsyncMock(),
            {"containerId": "11111"},
            AsyncMock(),
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(send.await_count, 1)
        self.assertEqual(send.call_args.args[0], "GET")

    async def test_meta_refreshes_aged_long_lived_token_and_persists_rotation(self):
        providers, send = self.providers(
            [{"access_token": "rotated-test-token"}, {"id": "11111"}, {"status": "IN_PROGRESS"}]
        )
        save = AsyncMock()
        await providers.publish(
            self.meta_account(lastRefreshAt=time.time() - 8 * 86400),
            post(deliveryAt=now()),
            [],
            save,
            None,
            AsyncMock(),
        )
        self.assertEqual(save.call_args.args[0]["token"], "rotated-test-token")
        self.assertEqual(send.call_args_list[0].kwargs["params"]["grant_type"], "th_refresh_token")

    async def test_meta_poll_network_failure_keeps_container_for_later(self):
        providers, _ = self.providers([ProviderError("Connection lost")])
        result = await providers.publish(
            self.meta_account(),
            post(deliveryAt=now()),
            [],
            AsyncMock(),
            {"containerId": "11111"},
            AsyncMock(),
        )
        self.assertEqual(result["status"], "processing")
        self.assertEqual(result["containerId"], "11111")

    async def test_meta_rejects_local_images_before_remote_requests(self):
        providers, send = self.providers([])
        with self.assertRaisesRegex(ValueError, "public JPEG"):
            await providers.publish(
                self.meta_account("instagram"),
                post(mediaIds=["image"], deliveryAt=now()),
                [{"bytes": JPEG, "mime": "image/jpeg"}],
                AsyncMock(),
            )
        send.assert_not_called()

    async def test_image_url_rejects_local_and_credential_urls(self):
        for url in [
            "http://example.org/a.jpg",
            "https://127.0.0.1/a.jpg",
            "https://server.local/a.jpg",
            "https://name:password@example.org/a.jpg",
            "https://example.org/a.jpg#fragment",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                image_url(url)


class NewJobTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = original.JobTests.asyncSetUp
    asyncTearDown = original.JobTests.asyncTearDown

    async def add_meta(self):
        self.providers.pause = AsyncMock()
        await self.store.put(
            "accounts",
            {
                "id": "meta",
                "platform": "threads",
                "name": "Threads",
                "remoteId": "12345",
                "credentials": Vault(KEY).encrypt({"token": "test-only-token", "userId": "12345"}),
            },
        )

    async def test_processing_is_durable_and_resumes_without_recreating_container(self):
        await self.add_meta()
        self.providers.send = AsyncMock(
            side_effect=[
                {"id": "11111"},
                {"status": "IN_PROGRESS"},
                {"status": "FINISHED"},
                {"id": "22222"},
                {},
            ]
        )
        await self.store.put("posts", post(accountIds=["meta", "a"]))
        await self.jobs.tick()
        saved = await self.store.get("posts", "p1")
        self.assertEqual(saved["status"], "processing")
        await self.jobs.recover()
        saved = await self.store.get("posts", "p1")
        self.assertEqual(saved["status"], "processing")
        await self.store.put("posts", {**saved, "nextCheckAt": "2020-01-01T00:00:00.000Z"})
        await self.jobs.tick()
        self.assertEqual((await self.store.get("posts", "p1"))["status"], "published")
        creations = [
            call for call in self.providers.send.call_args_list if call.args[1].endswith("/threads")
        ]
        self.assertEqual(len(creations), 1)

    async def test_polling_does_not_retry_other_failed_destinations(self):
        await self.add_meta()
        self.providers.publish = AsyncMock(
            side_effect=[
                {"status": "processing", "stage": "container", "containerId": "11111"},
                ProviderError("Denied", status=403),
                {"status": "published"},
            ]
        )
        await self.store.put("posts", post(accountIds=["meta", "a"]))
        await self.jobs.tick()
        saved = await self.store.get("posts", "p1")
        self.assertEqual(saved["status"], "processing")
        await self.store.put("posts", {**saved, "nextCheckAt": "2020-01-01T00:00:00.000Z"})
        await self.jobs.tick()
        self.assertEqual(self.providers.publish.await_count, 3)
        self.assertEqual((await self.store.get("posts", "p1"))["status"], "partial")

    async def test_poll_receipts_remain_bounded(self):
        await self.store.put("posts", post(accountIds=["a"]))
        for index in range(20):
            await self.jobs.receipt(
                "p1",
                {"accountId": "a", "status": "processing", "containerId": "11111", "check": index},
            )
        saved = await self.store.get("posts", "p1")
        self.assertEqual(len(saved["results"]), 8)
        self.assertEqual(saved["results"][-1]["check"], 19)

    async def test_restart_preserves_container_when_final_publish_is_unknown(self):
        await self.add_meta()
        await self.store.put(
            "posts",
            post(
                accountIds=["meta"],
                status="publishing",
                results=[
                    {
                        "accountId": "meta",
                        "status": "sending",
                        "containerId": "11111",
                        "stage": "publishing",
                    }
                ],
            ),
        )
        await self.jobs.recover()
        saved = await self.store.get("posts", "p1")
        self.assertEqual(saved["status"], "needs_review")
        self.assertEqual(saved["results"][-1]["containerId"], "11111")

    async def test_rejected_meta_publish_preserves_codes_and_prepares_new_container(self):
        await self.add_meta()
        self.providers.send = AsyncMock(
            side_effect=[
                {"id": "11111"},
                {"status": "FINISHED"},
                ProviderError("rejected", status=400, code=100, subcode=123),
                {"status": "ERROR"},
            ]
        )
        await self.store.put("posts", post(accountIds=["meta"]))
        await self.jobs.tick()
        saved = await self.store.get("posts", "p1")
        receipt = saved["results"][-1]
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["containerId"], "11111")
        self.assertEqual(
            (receipt["httpStatus"], receipt["providerCode"], receipt["providerSubcode"]),
            (400, 100, 123),
        )
        self.assertEqual(receipt["stage"], "rejected")
        self.providers.send = AsyncMock(
            side_effect=[{"id": "22222"}, {"status": "FINISHED"}, {"id": "33333"}, {}]
        )
        await self.store.put("posts", {**saved, "status": "queued"})
        await self.jobs.tick()
        retried = await self.store.get("posts", "p1")
        self.assertEqual(retried["status"], "published")
        self.assertEqual(retried["results"][-1]["containerId"], "22222")
        self.assertEqual(self.providers.send.await_count, 4)
        self.assertEqual(
            self.providers.send.call_args_list[2].kwargs["data"], {"creation_id": "22222"}
        )

    async def test_ambiguous_meta_publish_immediately_asks_user_to_check(self):
        await self.add_meta()
        self.providers.send = AsyncMock(
            side_effect=[
                {"id": "11111"},
                {"status": "FINISHED"},
                ProviderError("Interrupted", uncertain=True),
                {"status": "FINISHED"},
            ]
        )
        await self.store.put("posts", post(accountIds=["meta"]))
        await self.jobs.tick()
        saved = await self.store.get("posts", "p1")
        self.assertEqual(saved["status"], "needs_review")
        self.assertEqual(saved["results"][-1]["status"], "uncertain")
        self.assertIn("Check Threads", saved["results"][-1]["message"])
        self.assertIsNone(saved["nextCheckAt"])
        self.providers.send.reset_mock()
        await self.jobs.tick()
        self.providers.send.assert_not_awaited()


class NewApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = original.ApiTests.asyncSetUp
    asyncTearDown = original.ApiTests.asyncTearDown
    create_post = original.ApiTests.create_post

    async def test_public_jpeg_import_returns_metadata_without_source_url(self):
        self.send.return_value = JPEG
        response = await self.client.post(
            "/api/media/url", json={"url": "https://example.org/photo.jpg"}
        )
        self.assertEqual(response.status, 201)
        media = await response.json()
        self.assertTrue(media["publicSource"])
        self.assertNotIn("sourceUrl", media)
        image_path = self.jobs.media_dir / (media["id"] + ".bin")
        self.assertTrue(image_path.exists())
        self.assertEqual(image_path.stat().st_mode & 0o777, 0o600)
        fetched = await self.jobs.image(media["id"])
        self.assertEqual(fetched["bytes"], JPEG)
        self.assertTrue(image_path.exists())
        await self.store.put(
            "accounts",
            {
                "id": "ig",
                "name": "Instagram",
                "platform": "instagram",
                "remoteId": "12345",
                "credentials": Vault(KEY).encrypt({"token": "test-only-token"}),
            },
        )
        self.assertEqual(
            (
                await self.create_post(
                    status="now", accountIds=["ig", "test-bluesky"], mediaIds=[media["id"]]
                )
            ).status,
            202,
        )
        state = await (await self.client.get("/api/state")).json()
        self.assertTrue(state["media"][0]["publicSource"])
        self.assertNotIn("sourceUrl", state["media"][0])

    async def test_public_url_restriction_and_non_jpeg_rejection(self):
        self.assertEqual(
            (
                await self.client.post(
                    "/api/media/url", json={"url": "https://127.0.0.1/image.jpg"}
                )
            ).status,
            400,
        )
        self.send.assert_not_called()
        self.send.return_value = PNG
        self.assertEqual(
            (
                await self.client.post("/api/media/url", json={"url": "https://example.org/a.png"})
            ).status,
            400,
        )

    async def test_webhook_url_never_leaves_account_connection(self):
        self.send.return_value = {
            "id": "123456789012345678",
            "type": 1,
            "guild_id": "98765",
            "channel_id": "56789",
            "name": "updates",
        }
        response = await self.client.post(
            "/api/accounts", json={"platform": "discord", "secret": WEBHOOK}
        )
        self.assertEqual(response.status, 201)
        for endpoint in ["/api/state", "/api/export"]:
            body = await (await self.client.get(endpoint)).text()
            self.assertNotIn("test_only_webhook_token", body)
            self.assertNotIn("/api/webhooks/", body)

    async def test_processing_post_cannot_be_deleted_or_disconnected(self):
        await self.store.put(
            "posts",
            post(
                accountIds=["test-bluesky"],
                status="processing",
                results=[{"accountId": "test-bluesky", "status": "processing"}],
            ),
        )
        self.assertEqual((await self.client.delete("/api/posts/p1")).status, 400)
        self.assertEqual((await self.client.delete("/api/accounts/test-bluesky")).status, 400)

    async def test_linkedin_connect_reconnect_and_export_keep_token_private(self):
        self.send.return_value = {"sub": "member123", "name": "Test Member"}
        response = await self.client.post(
            "/api/accounts", json={"platform": "linkedin", "secret": "PRIVATE_LINKEDIN_TEST_TOKEN"}
        )
        self.assertEqual(response.status, 201)
        account = await response.json()
        self.assertNotIn("paused", account)
        self.assertEqual(account["platform"], "linkedin")
        defaults = await (await self.client.get(f"/api/accounts/{account['id']}/connection")).json()
        self.assertEqual(defaults, {"values": {}, "savedFields": ["secret"]})
        response = await self.client.post(
            "/api/accounts", json={"id": account["id"], "platform": "linkedin", "secret": ""}
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(self.send.await_count, 2)
        for path in ("/api/state", "/api/export"):
            self.assertNotIn(
                "PRIVATE_LINKEDIN_TEST_TOKEN", await (await self.client.get(path)).text()
            )
        response = await self.create_post(
            status="scheduled", scheduledAt=original.future(), accountIds=[account["id"]]
        )
        self.assertEqual(response.status, 201)

    async def test_catalog_has_ten_live_platforms(self):
        state = await (await self.client.get("/api/state")).json()
        self.assertEqual(
            {p["id"] for p in state["platforms"]},
            {
                "youtube",
                "bluesky",
                "mastodon",
                "x",
                "instagram",
                "threads",
                "discord",
                "linkedin",
                "telegram",
                "facebook",
            },
        )
