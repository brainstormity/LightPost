import asyncio
from pathlib import Path

from .providers import ProviderError, destination_post, image_url, now, validate_post

BUSY = {"queued", "publishing", "scheduled", "processing"}
DONE = {"published", "simulated"}


class PostingCancelled(Exception):
    pass


def delivery_status(statuses):
    if "uncertain" in statuses:
        return "needs_review"
    if "processing" in statuses:
        return "processing"
    if "failed" in statuses:
        return "partial" if any(s in DONE for s in statuses) else "failed"
    return "simulated" if all(s == "simulated" for s in statuses) else "published"


class Scheduler:
    def __init__(self, store, providers, media_dir, interval=3):
        self.store, self.providers = store, providers
        self.media_dir, self.interval = Path(media_dir), interval
        self.post_lock = asyncio.Lock()
        self.result_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(3)
        self.account_locks = {}
        self.active = set()
        self.task = None
        self.stopping = False
        self.wake = asyncio.Event()

    async def recover(self):
        for post in await self.store.all("posts"):
            if post["status"] in ("publishing", "processing"):
                results = post.get("results", [])
                for id in post["accountIds"]:
                    latest = next((r for r in reversed(results) if r["accountId"] == id), None)
                    resumable = (
                        latest
                        and latest["status"] == "processing"
                        and latest.get("containerId")
                        and latest.get("stage") in ("container", "verifying", "youtube-upload")
                    )
                    account = await self.store.get("accounts", id)
                    if (
                        latest
                        and latest.get("containerId")
                        and account
                        and account["platform"] == "youtube"
                        and latest.get("stage") in ("youtube-upload", "verifying")
                        and latest["status"] in ("sending", "processing")
                    ):
                        resumable = True
                        if latest["status"] == "sending":
                            results.append({**latest, "status": "processing", "at": now()})
                    if not latest or latest["status"] not in DONE | {"failed"} and not resumable:
                        results.append(
                            {
                                **(latest or {}),
                                "accountId": id,
                                "status": "uncertain",
                                "message": "Server stopped during delivery. Check the platform before retrying.",
                                "at": now(),
                            }
                        )
                statuses = {r["accountId"]: r["status"] for r in results}
                post.update(status=delivery_status(statuses.values()), results=results)
                await self.store.put("posts", post)
            elif post["status"] in ("scheduled", "queued"):
                try:
                    await self.validate(post)
                except ValueError as error:
                    await self.store.put(
                        "posts", {**post, "status": "blocked", "message": str(error)}
                    )

    async def validate(self, post):
        images = [await self.image(id) for id in post.get("mediaIds", [])]
        if post.get("videoId"):
            await self.video(post["videoId"])
        for id in post["accountIds"]:
            account = await self.store.get("accounts", id)
            if not account:
                raise ValueError("An account was disconnected. Select available accounts.")
            if post.get("videoId") and account["platform"] != "youtube":
                raise ValueError("Video posts support YouTube accounts only.")
            target = destination_post(account, post)
            validate_post(account, target)
            target_images = images if target.get("mediaIds") else []
            if (
                not account.get("demo")
                and account["platform"] in ("telegram", "facebook")
                and any(image["mime"] not in ("image/jpeg", "image/png") for image in target_images)
            ):
                raise ValueError("Telegram and Facebook Pages images must be JPEG or PNG.")
            if (
                not account.get("demo")
                and account["platform"] in ("instagram", "threads")
                and target_images
            ):
                if (
                    len(target_images) != 1
                    or target_images[0]["mime"] != "image/jpeg"
                    or not target_images[0].get("sourceUrl")
                ):
                    raise ValueError(
                        "Instagram and Threads images require one public JPEG. Use Add image URL."
                    )

    async def image(self, id):
        media = await self.store.get("media", id)
        if not media:
            raise ValueError("An attached image is missing. Remove or upload it again.")
        path = self.media_dir / (id + ".bin")
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except OSError:
            if not media.get("sourceUrl"):
                raise ValueError("An attached image file is missing.") from None
            content = await self.providers.send(
                "GET", image_url(media["sourceUrl"]), _binary=True, _limit=1024 * 1024
            )
            if (
                not isinstance(content, bytes)
                or len(content) > 1024 * 1024
                or not content.startswith(b"\xff\xd8\xff")
            ):
                raise ValueError("The image URL must provide a JPEG no larger than 1 MB.")
            await asyncio.to_thread(path.write_bytes, content)
            path.chmod(0o600)
        mime = media["mime"]
        if len(content) > 1024 * 1024:
            raise ValueError("Images must be 1 MB or smaller. Resize and upload again.")
        return {
            "bytes": content,
            "mime": mime,
            "name": media["name"],
            "alt": media.get("alt", ""),
            "sourceUrl": media.get("sourceUrl"),
        }

    async def video(self, identity):
        if not isinstance(identity, str):
            raise ValueError("Choose a saved MP4 video.")
        video = await self.store.get("videos", identity)
        if not video:
            raise ValueError("The attached video is missing. Upload it again.")
        path = self.media_dir / (video["id"] + ".mp4")
        from .youtube import MAX_VIDEO

        if (
            not path.is_file()
            or path.stat().st_size != video["size"]
            or not 0 < video["size"] <= MAX_VIDEO
        ):
            raise ValueError("The attached video file is missing or changed. Upload it again.")
        return {**video, "path": str(path)}

    async def receipt(self, post_id, result):
        async with self.result_lock:
            post = await self.store.get("posts", post_id)
            if post.get("cancelRequested") and result["status"] not in DONE:
                return
            post.setdefault("results", []).append({**result, "at": now()})
            counts, recent = {}, []
            for receipt in reversed(post["results"]):
                id = receipt["accountId"]
                counts[id] = counts.get(id, 0) + 1
                if counts[id] <= 8:
                    recent.append(receipt)
            post["results"] = list(reversed(recent))
            await self.store.put("posts", post)

    async def destination(self, post, id, images):
        account_lock = self.account_locks.setdefault(id, asyncio.Lock())
        async with account_lock, self.slots:
            current = await self.store.get("posts", post["id"])
            if current.get("cancelRequested"):
                return
            previous = next(
                (r for r in reversed(post.get("results", [])) if r["accountId"] == id), None
            )
            checkpoint_state = {
                k: previous[k]
                for k in (
                    "containerId",
                    "containerCreatedAt",
                    "stage",
                    "verificationStartedAt",
                    "retryRequestHandledAt",
                    "photoIds",
                    "photoMediaIds",
                    "remoteId",
                )
                if previous and k in previous
            }
            await self.receipt(
                post["id"], {**checkpoint_state, "accountId": id, "status": "sending"}
            )
            try:
                account = await self.store.get("accounts", id)
                if not account:
                    raise ValueError("Account was disconnected.")

                async def save_credentials(credentials):
                    await self.store.put(
                        "accounts",
                        {**account, "credentials": self.providers.vault.encrypt(credentials)},
                    )

                async def checkpoint(value):
                    current = await self.store.get("posts", post["id"])
                    if current.get("cancelRequested"):
                        raise PostingCancelled()
                    checkpoint_state.update(value)
                    await self.receipt(post["id"], {**value, "accountId": id})

                post = destination_post(account, post)
                if account["platform"] == "youtube":
                    post = {**post, "_video": await self.video(post.get("videoId"))}
                images = images if post.get("mediaIds") else []

                if account["platform"] in (
                    "youtube",
                    "instagram",
                    "threads",
                    "linkedin",
                    "telegram",
                    "facebook",
                    "x",
                ) and not account.get("demo"):
                    result = await self.providers.publish(
                        account, post, images, save_credentials, previous, checkpoint
                    )
                else:
                    result = await self.providers.publish(account, post, images, save_credentials)
            except PostingCancelled:
                return
            except ProviderError as error:
                if getattr(error, "reconnect_required", False):
                    current_account = await self.store.get("accounts", id)
                    if current_account:
                        await self.store.put(
                            "accounts",
                            {
                                **current_account,
                                "connectionStatus": "reconnect_required",
                                "connectionMessage": str(error),
                            },
                        )
                result = {
                    "status": "uncertain" if error.uncertain else "failed",
                    "message": str(error),
                }
                for name, value in (
                    ("httpStatus", error.status),
                    ("providerCode", error.code),
                    ("providerSubcode", error.subcode),
                ):
                    if value:
                        result[name] = value
                if error.uncertain or checkpoint_state.get("stage") in (
                    "publishing",
                    "youtube-upload",
                    "verifying",
                ):
                    result = {**checkpoint_state, **result}
            except ValueError as error:
                result = {**checkpoint_state, "status": "failed", "message": str(error)}
            except Exception:
                result = {
                    **checkpoint_state,
                    "status": "uncertain",
                    "message": "No reliable delivery receipt. Check the platform before retrying.",
                }
            await self.receipt(post["id"], {**result, "accountId": id})

    async def deliver(self, id):
        # Claim is serialized with API edits/deletes/retries. The whole service owns one DB lock.
        async with self.post_lock:
            post = await self.store.get("posts", id)
            if (
                not post
                or post["status"] not in ("queued", "scheduled", "processing")
                or id in self.active
            ):
                return
            if post["status"] == "scheduled" and post["scheduledAt"] > now():
                return
            polling = post["status"] == "processing"
            try:
                await self.validate(post)
            except ValueError as error:
                await self.store.put("posts", {**post, "status": "blocked", "message": str(error)})
                return
            post.update(status="publishing", deliveryAt=post.get("deliveryAt") or now())
            await self.store.put("posts", post)
            self.active.add(id)
        try:
            images = [await self.image(media_id) for media_id in post.get("mediaIds", [])]
            latest = {r["accountId"]: r for r in post.get("results", [])}
            await asyncio.gather(
                *(
                    self.destination(post, account_id, images)
                    for account_id in post["accountIds"]
                    if latest.get(account_id, {}).get("status") not in DONE
                    and (not polling or latest.get(account_id, {}).get("status") == "processing")
                )
            )
            async with self.result_lock:
                post = await self.store.get("posts", id)
                if post.get("cancelRequested"):
                    return
                latest = {r["accountId"]: r for r in post.get("results", [])}
                statuses = [
                    latest.get(account_id, {}).get("status", "uncertain")
                    for account_id in post["accountIds"]
                ]
                status = delivery_status(statuses)
                from datetime import datetime, timedelta, timezone

                next_check = (
                    (
                        datetime.now(timezone.utc)
                        + timedelta(
                            seconds=max(
                                [3, *(receipt.get("retryAfter", 3) for receipt in latest.values())]
                            )
                        )
                    )
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )
                await self.store.put(
                    "posts",
                    {
                        **post,
                        "status": status,
                        "nextCheckAt": next_check if status == "processing" else None,
                        "finishedAt": now(),
                    },
                )
        finally:
            self.active.discard(id)

    async def tick(self):
        posts = await self.store.due(now())
        await asyncio.gather(*(self.deliver(post["id"]) for post in posts))

    async def loop(self):
        while not self.stopping:
            try:
                await self.tick()
            except Exception:
                # Do not log arbitrary provider payloads or credentials.
                import logging

                logging.getLogger("lightpost").error(
                    "Scheduler tick failed; pending jobs remain in SQLite."
                )
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def start(self):
        await self.recover()
        self.task = asyncio.create_task(self.loop())

    async def close(self):
        self.stopping = True
        self.wake.set()
        if self.task:
            # Let in-flight receipts finish. If forced to stop, recover() requests manual review.
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=35)
            except asyncio.TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
