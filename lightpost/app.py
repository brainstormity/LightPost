import asyncio
import hmac
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

from .config import Config
from .jobs import BUSY, DONE, Scheduler
from .providers import (
    PLATFORMS,
    ROADMAP,
    SUPPORTED,
    Providers,
    ProviderError,
    Transport,
    image_url,
    now,
)
from .security import Vault, load_api_key, new_api_key, save_api_key
from .store import Store


KEYS = {
    name: web.AppKey(name, object)
    for name in [
        "config",
        "sessions",
        "attempts",
        "store",
        "vault",
        "providers",
        "jobs",
        "automation",
        "remote",
        "test_transport",
    ]
}


def clean_account(account):
    fields = ("id", "platform", "name", "handle", "remoteId", "demo", "createdAt", "limit")
    return {
        **{k: account[k] for k in fields if k in account},
        **({"name": "X (Twitter)"} if account.get("platform") == "x" else {}),
    }


def iso_time(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Choose a date and time with a timezone.") from None


async def read_json(request):
    try:
        value = await request.json()
    except (ValueError, UnicodeError):
        raise ValueError("Invalid JSON request.") from None
    if not isinstance(value, dict):
        raise ValueError("Send a JSON object.")
    return value


def authorize_api(request):
    """Automation endpoints (/api/v1, /mcp) use an API key, not the browser session.

    The key goes in an Authorization: Bearer (or X-API-Key) header. For tools that cannot send
    headers, /mcp/<key> carries it in the address instead.
    """
    app = request.app
    if not app[KEYS["automation"]]["enabled"]:
        raise web.HTTPForbidden(
            text="API access is turned off. Turn it on in LightPost under Settings > Automation."
        )
    header = request.headers.get("Authorization", "")
    supplied = header[7:].strip() if header[:7].lower() == "bearer " else ""
    supplied = supplied or request.headers.get("X-API-Key", "").strip()
    if not supplied and request.path.startswith("/mcp/"):
        supplied = request.path[5:].strip("/")
    expected = app[KEYS["config"]].api_key
    if (
        not supplied
        or not expected
        or not hmac.compare_digest(supplied.encode(), expected.encode())
    ):
        raise web.HTTPUnauthorized(text="Missing or invalid API key.")


@web.middleware
async def guard(request, handler):
    config = request.app[KEYS["config"]]
    try:
        local = urlsplit("http://" + request.host).hostname in ("localhost", "127.0.0.1", "::1")
        automation = request.path == "/mcp" or request.path.startswith(("/mcp/", "/api/v1/"))
        if not local and not config.password and not automation:
            # Reached through a public address (a tunnel or proxy) with no dashboard password:
            # only the key-protected API and MCP endpoints are available, nothing else.
            raise web.HTTPForbidden(
                text="The dashboard is not available on public addresses. Open LightPost on the "
                "computer it runs on, or set ADMIN_PASSWORD."
            )
        if automation:
            # A bearer key is never sent by a browser on its own, so the loopback-hostname and
            # cross-site checks below are not needed here.
            authorize_api(request)
        elif request.path.startswith("/api/"):
            origin = request.headers.get("Origin")
            if (
                request.method not in ("GET", "HEAD")
                and origin
                and urlsplit(origin).netloc != request.host
            ):
                raise web.HTTPForbidden(text="Cross-origin writes are blocked.")
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                raise web.HTTPForbidden(text="Cross-site API requests are blocked.")
            authenticated = (
                not config.password
                or request.app[KEYS["sessions"]].get(request.cookies.get("lightpost_session"), 0)
                > time.monotonic()
            )
            if request.path not in ("/api/session", "/api/login") and not authenticated:
                raise web.HTTPUnauthorized(text="Sign in to your workspace.")
        response = await handler(request)
    except web.HTTPException as error:
        response = web.json_response(
            {"error": error.text if error.status < 500 else "Server error."}, status=error.status
        )
    except (ValueError, ProviderError) as error:
        response = web.json_response({"error": str(error)}, status=400)
    except Exception:
        response = web.json_response(
            {"error": "Unable to complete this request. No changes were confirmed."}, status=500
        )
    response.headers.update(
        {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            "Cache-Control": "no-store",
        }
    )
    return response


async def session(request):
    return web.json_response(
        {
            "authenticated": not request.app[KEYS["config"]].password
            or request.app[KEYS["sessions"]].get(request.cookies.get("lightpost_session"), 0)
            > time.monotonic(),
            "passwordRequired": bool(request.app[KEYS["config"]].password),
        }
    )


async def login(request):
    data = await read_json(request)
    app = request.app
    stamp = time.monotonic()
    app[KEYS["attempts"]][:] = [t for t in app[KEYS["attempts"]] if t > stamp - 600]
    if len(app[KEYS["attempts"]]) >= 10:
        raise web.HTTPTooManyRequests(text="Too many attempts. Wait ten minutes.")
    app[KEYS["attempts"]].append(stamp)
    if app[KEYS["config"]].password and not hmac.compare_digest(
        str(data.get("password", "")).encode(), app[KEYS["config"]].password.encode()
    ):
        raise web.HTTPUnauthorized(text="Incorrect password.")
    for key in list(app[KEYS["sessions"]]):
        if app[KEYS["sessions"]][key] <= stamp:
            del app[KEYS["sessions"]][key]
    if len(app[KEYS["sessions"]]) > 100:
        app[KEYS["sessions"]].clear()
    token = secrets.token_urlsafe(32)
    app[KEYS["sessions"]][token] = stamp + 86400
    response = web.json_response({"ok": True})
    response.set_cookie(
        "lightpost_session",
        token,
        max_age=86400,
        httponly=True,
        samesite="Strict",
        secure=app[KEYS["config"]].secure_cookie,
    )
    return response


async def logout(request):
    request.app[KEYS["sessions"]].pop(request.cookies.get("lightpost_session"), None)
    response = web.json_response({"ok": True})
    response.del_cookie("lightpost_session")
    return response


async def state(request):
    store = request.app[KEYS["store"]]
    posts, total = await store.posts()
    async with store.db.execute(
        "SELECT json_extract(value, '$.status'), count(*) FROM records WHERE kind='posts' AND COALESCE(json_extract(value, '$.demo'),0)=0 AND json_extract(value, '$.status')!='simulated' GROUP BY 1"
    ) as cursor:
        counts = dict(await cursor.fetchall())
    # Select only the image metadata needed by the browser.
    async with store.db.execute(
        "SELECT id, json_extract(value, '$.name'), json_extract(value, '$.alt'), json_extract(value, '$.sourceUrl') IS NOT NULL FROM records WHERE kind='media'"
    ) as cursor:
        media = [
            {"id": row[0], "name": row[1], "alt": row[2] or "", "publicSource": bool(row[3])}
            for row in await cursor.fetchall()
        ]
    return web.json_response(
        {
            "accounts": [
                clean_account(a) for a in await store.all("accounts") if not a.get("demo")
            ],
            "posts": posts,
            "total": total,
            "counts": counts,
            "media": media,
            "settings": await store.get("settings", "workspace")
            or {"name": "My workspace", "timezone": "UTC"},
            "platforms": PLATFORMS,
            "roadmap": ROADMAP,
            "runtime": "Python async · SQLite",
            "authEnabled": bool(request.app[KEYS["config"]].password),
        }
    )


async def list_posts(request):
    try:
        offset = max(0, int(request.query.get("offset", 0)))
    except ValueError:
        raise ValueError("Invalid page.") from None
    posts, total = await request.app[KEYS["store"]].posts(request.query.get("status", ""), offset)
    return web.json_response({"posts": posts, "total": total})


def saved_connection_fields(app, account):
    credentials = app[KEYS["vault"]].decrypt(account["credentials"])
    platform = account["platform"]
    if platform == "bluesky":
        return {
            "handle": credentials["identifier"],
            "instance": credentials["instance"],
            "secret": credentials["password"],
        }
    if platform == "facebook":
        return {"pageId": credentials["pageId"], "secret": credentials["token"]}
    if platform == "telegram":
        return {"chatId": credentials["chatId"], "secret": credentials["token"]}
    if platform == "mastodon":
        return {"instance": credentials["instance"], "secret": credentials["token"]}
    if platform == "discord":
        webhook = credentials["webhook"]
        if credentials.get("threadId"):
            webhook += "?thread_id=" + credentials["threadId"]
        return {"secret": webhook}
    return {
        field: credentials[key]
        for field, key in {
            "secret": "token",
            "apiKey": "apiKey",
            "apiSecret": "apiSecret",
            "tokenSecret": "tokenSecret",
        }.items()
        if key in credentials
    }


async def connection_defaults(request):
    account = await request.app[KEYS["store"]].get("accounts", request.match_info["id"])
    if not account or account.get("demo") or account["platform"] not in SUPPORTED:
        raise ValueError("Choose an existing live account.")
    fields = saved_connection_fields(request.app, account)
    # Secrets never leave the server. Only return public fields and saved-field names.
    return web.json_response(
        {
            "values": {
                k: v for k, v in fields.items() if k in ("handle", "instance", "chatId", "pageId")
            },
            "savedFields": [k for k, v in fields.items() if v],
        }
    )


async def connect(request):
    data = await read_json(request)
    app, store = request.app, request.app[KEYS["store"]]
    if data.get("id"):
        existing = await store.get("accounts", data["id"])
        if not existing or existing.get("demo") or existing["platform"] != data.get("platform"):
            raise ValueError("Reconnect an existing live account of the same platform.")
        saved = saved_connection_fields(app, existing)
        for field, value in saved.items():
            if not str(data.get(field, "")).strip():
                data[field] = value
    # Verify outside the mutation lock; never persist unverified credentials.
    account = await app[KEYS["providers"]].connect(data)
    async with app[KEYS["jobs"]].post_lock:
        existing = await store.get("accounts", data.get("id", ""))
        if data.get("id"):
            if not existing or existing.get("demo") or existing["platform"] != account["platform"]:
                raise ValueError("Reconnect an existing live account of the same platform.")
            if existing["remoteId"] != account["remoteId"]:
                raise ValueError("Reconnect the same identity, or add it as a new account.")
            if (
                account["platform"] == "mastodon"
                and app[KEYS["vault"]].decrypt(existing["credentials"])["instance"]
                != app[KEYS["vault"]].decrypt(account["credentials"])["instance"]
            ):
                raise ValueError("Reconnect the same Mastodon instance.")
            if any(
                existing["id"] in p["accountIds"] and p["status"] == "publishing"
                for p in await store.all("posts")
            ):
                raise ValueError(
                    "Wait for this account's current delivery to finish before reconnecting."
                )
        for other in await store.all("accounts"):
            if (
                not other.get("demo")
                and other["platform"] == account["platform"]
                and other.get("remoteId") == account.get("remoteId")
                and other["id"] != data.get("id")
            ):
                # Mastodon IDs are only unique within an instance.
                if (
                    account["platform"] != "mastodon"
                    or app[KEYS["vault"]].decrypt(other["credentials"])["instance"]
                    == app[KEYS["vault"]].decrypt(account["credentials"])["instance"]
                ):
                    raise ValueError(
                        "This account is already connected. Use Reconnect to replace its credentials."
                    )
        account.update(
            id=existing["id"] if existing else str(uuid4()),
            createdAt=existing.get("createdAt", now()) if existing else now(),
        )
        await store.put("accounts", account)
    return web.json_response(clean_account(account), status=201)


async def disconnect(request):
    store = request.app[KEYS["store"]]
    id = request.match_info["id"]
    async with request.app[KEYS["jobs"]].post_lock:
        if any(id in p["accountIds"] and p["status"] in BUSY for p in await store.all("posts")):
            raise ValueError("Cancel scheduled posts or wait for delivery before disconnecting.")
        await store.remove("accounts", id)
    return web.json_response({"ok": True})


async def write_post(app, data):
    """Create, update, schedule or publish a post. Shared by the UI, REST API and MCP."""
    store, jobs = app[KEYS["store"]], app[KEYS["jobs"]]
    async with jobs.post_lock:
        old = await store.get("posts", data.get("id", ""))
        if data.get("id") and not old:
            raise ValueError("Post not found.")
        key = data.get("idempotencyKey")
        if key is not None and (not isinstance(key, str) or not 0 < len(key) <= 200):
            raise ValueError("idempotencyKey must be a string of 1 to 200 characters.")
        if key and not old and (replay := await store.post_with_key(key)):
            return replay, 200
        if old and old["status"] not in ("draft", "scheduled", "blocked"):
            raise ValueError("Duplicate this post to change it, or retry its failed destinations.")
        text = data.get("text", "")
        if (
            not isinstance(text, str)
            or len(text) > 10000
            or (not text.strip() and not data.get("mediaIds"))
        ):
            raise ValueError(
                "Write a post or attach an image. Text can be up to 10,000 characters."
            )
        ids = data.get("accountIds", [])
        if not isinstance(ids, list) or not ids:
            raise ValueError("Choose at least one connected account.")
        for id in ids:
            if (
                not isinstance(id, str)
                or not (account := await store.get("accounts", id))
                or account.get("demo")
            ):
                raise ValueError("Choose at least one connected account.")
        ids = list(dict.fromkeys(ids))
        if len(ids) > 25:
            raise ValueError("Choose up to 25 accounts per post.")
        media_ids = data.get("mediaIds", [])
        if (
            not isinstance(media_ids, list)
            or len(media_ids) > 4
            or any(not isinstance(id, str) for id in media_ids)
        ):
            raise ValueError("Choose up to four images.")
        options = data.get("contentOptions", {})
        if not isinstance(options, dict) or any(
            not isinstance(value, dict)
            or any(k not in ("text", "image") or not isinstance(v, bool) for k, v in value.items())
            for value in options.values()
        ):
            raise ValueError("Choose valid Text and Image options.")
        options = {id: options[id] for id in ids if id in options}
        status = data.get("status")
        if status not in ("draft", "now", "scheduled"):
            raise ValueError("Choose draft, publish now or schedule.")
        scheduled_at = iso_time(data.get("scheduledAt")) if status == "scheduled" else None
        if scheduled_at and scheduled_at <= now():
            raise ValueError("Choose a future date and time.")
        post = {
            "id": old["id"] if old else str(uuid4()),
            "text": text.strip(),
            "accountIds": ids,
            "mediaIds": list(dict.fromkeys(media_ids)),
            "status": "queued" if status == "now" else status,
            "scheduledAt": scheduled_at,
            "createdAt": old.get("createdAt", now()) if old else now(),
            "updatedAt": now(),
            "results": [],
            "xAllowLinks": data.get("xAllowLinks") is True,
            "contentOptions": options,
        }
        if key := (old or {}).get("idempotencyKey") or key:
            post["idempotencyKey"] = key
        # Drafts can exceed platform limits; deliveries cannot.
        if status != "draft":
            x_accounts = [await store.get("accounts", id) for id in ids]
            if (
                any(a["platform"] == "x" for a in x_accounts)
                and data.get("xCostConfirmed") is not True
            ):
                raise ValueError(
                    "Confirm the estimated X (Twitter) API cost before scheduling or publishing."
                )
            await jobs.validate(post)
        else:
            for id in media_ids:
                await jobs.image(id)
        await store.put("posts", post)
    return post, 202 if status == "now" else 201


async def save_post(request):
    post, status = await write_post(request.app, await read_json(request))
    return web.json_response(post, status=status)


async def retry_post(app, id, data):
    store, jobs = app[KEYS["store"]], app[KEYS["jobs"]]
    async with jobs.post_lock:
        post = await store.get("posts", id)
        if not post or post["status"] not in ("failed", "partial", "needs_review", "cancelled"):
            raise ValueError("This post does not have failed deliveries to retry.")
        if post["id"] in jobs.active:
            raise ValueError("The previous request is still finishing. Retry shortly.")
        latest = {r["accountId"]: r["status"] for r in post.get("results", [])}
        accounts = [
            await store.get("accounts", id)
            for id in post["accountIds"]
            if latest.get(id) not in DONE
        ]
        if (
            any(a and a["platform"] == "x" for a in accounts)
            and data.get("xCostConfirmed") is not True
        ):
            raise ValueError("Confirm the estimated X (Twitter) API cost before retrying.")
        await jobs.validate(post)
        post["status"] = "queued"
        post["retryRequestedAt"] = time.time()
        post.pop("cancelRequested", None)
        post.pop("message", None)
        await store.put("posts", post)
    return post


async def retry(request):
    data = await read_json(request)
    return web.json_response(
        await retry_post(request.app, request.match_info["id"], data), status=202
    )


async def cancel_post(app, id):
    store, jobs = app[KEYS["store"]], app[KEYS["jobs"]]
    async with jobs.post_lock, jobs.result_lock:
        post = await store.get("posts", id)
        if not post or post["status"] not in ("scheduled", "queued", "publishing", "processing"):
            raise ValueError("Only scheduled or active posting can be cancelled.")
        if post["status"] in ("scheduled", "queued"):
            post.update(status="draft", scheduledAt=None)
        else:
            post.update(
                status="cancelled",
                cancelRequested=True,
                nextCheckAt=None,
                finishedAt=now(),
                message="Further posting attempts stopped. Posts already published remain online. Requests already sent may still finish; check the platform.",
            )
            latest = {r["accountId"]: r for r in post.get("results", [])}
            for account_id in post["accountIds"]:
                receipt = latest.get(account_id, {})
                if receipt.get("status") in DONE | {"failed", "uncertain"}:
                    continue
                sent = receipt.get("stage") in ("publishing", "verifying") or (
                    receipt.get("status") == "sending" and not receipt.get("stage")
                )
                post.setdefault("results", []).append(
                    {
                        **receipt,
                        "accountId": account_id,
                        "status": "uncertain" if sent else "cancelled",
                        "message": "Stopped waiting. A request was already sent; check the platform."
                        if sent
                        else "Posting cancelled before publication.",
                        "at": now(),
                    }
                )
        await store.put("posts", post)
    return post


async def cancel(request):
    return web.json_response(await cancel_post(request.app, request.match_info["id"]))


async def remove_post(app, id):
    store, jobs = app[KEYS["store"]], app[KEYS["jobs"]]
    async with jobs.post_lock:
        post = await store.get("posts", id)
        if post and post["status"] in ("queued", "publishing", "scheduled", "processing"):
            raise ValueError("Cancel the schedule or wait for delivery before deleting.")
        await store.remove("posts", id)


async def delete_post(request):
    await remove_post(request.app, request.match_info["id"])
    return web.json_response({"ok": True})


def image_mime(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Upload a JPG, PNG or WebP image.")


async def upload(request):
    try:
        reader = await request.multipart()
    except (ValueError, AssertionError):
        raise ValueError("Send an image as multipart form data.") from None
    part = await reader.next()
    if not part or part.name != "file":
        raise ValueError("Choose an image file.")
    content = bytearray()
    while chunk := await part.read_chunk(65536):
        content.extend(chunk)
        if len(content) > 1024 * 1024:
            raise ValueError("Resize images to 1 MB or smaller before uploading.")
    mime = image_mime(content)
    media = await save_image(request.app, content, mime, Path(part.filename or "Image").name[:150])
    return web.json_response(media, status=201)


async def import_image(request):
    data = await read_json(request)
    url = image_url(data.get("url", ""))
    # PublicResolver validates and pins DNS; redirects and private network addresses are blocked.
    content = await request.app[KEYS["providers"]].send(
        "GET", url, _binary=True, _limit=1024 * 1024
    )
    if not isinstance(content, bytes) or len(content) > 1024 * 1024:
        raise ValueError("Use an image no larger than 1 MB.")
    mime = image_mime(content)
    if mime != "image/jpeg":
        raise ValueError("Use a JPEG image URL for Instagram and Threads.")
    media = await save_image(request.app, content, mime, "Public image.jpg", sourceUrl=url)
    return web.json_response(
        {k: v for k, v in {**media, "publicSource": True}.items() if k != "sourceUrl"}, status=201
    )


async def save_image(app, content, mime, name, **extra):
    id = str(uuid4())
    media = {
        "id": id,
        "name": name,
        "mime": mime,
        "size": len(content),
        "alt": "",
        "createdAt": now(),
        **extra,
    }
    path = app[KEYS["jobs"]].media_dir / (id + ".bin")
    await asyncio.to_thread(path.write_bytes, content)
    path.chmod(0o600)
    await app[KEYS["store"]].put("media", media)
    return media


async def media_file(request):
    image = await request.app[KEYS["jobs"]].image(request.match_info["id"])
    return web.Response(
        body=image["bytes"], content_type=image["mime"], headers={"Cache-Control": "no-store"}
    )


async def set_media_alt(app, id, alt):
    media = await app[KEYS["store"]].get("media", id)
    if not media:
        raise web.HTTPNotFound(text="Image not found.")
    if not isinstance(alt, str) or len(alt) > 1000:
        raise ValueError("Alt text must be 1,000 characters or fewer.")
    # Avoid changing an attachment already queued, sent, or currently sending.
    async with app[KEYS["jobs"]].post_lock:
        if any(
            media["id"] in p.get("mediaIds", []) and p["status"] != "draft"
            for p in await app[KEYS["store"]].all("posts")
        ):
            raise ValueError(
                "This image belongs to a saved delivery. Upload another copy to change its alt text."
            )
        await app[KEYS["store"]].put("media", {**media, "alt": alt})


async def media_alt(request):
    data = await read_json(request)
    await set_media_alt(request.app, request.match_info["id"], data.get("alt", ""))
    return web.json_response({"ok": True})


async def remove_media(app, id):
    async with app[KEYS["jobs"]].post_lock:
        if any(id in p.get("mediaIds", []) for p in await app[KEYS["store"]].all("posts")):
            raise ValueError("Remove this image from saved posts first.")
        await app[KEYS["store"]].remove("media", id)
        (app[KEYS["jobs"]].media_dir / (id + ".bin")).unlink(missing_ok=True)


async def delete_media(request):
    await remove_media(request.app, request.match_info["id"])
    return web.json_response({"ok": True})


async def settings(request):
    data = await read_json(request)
    try:
        ZoneInfo(data.get("timezone", ""))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("Choose a valid IANA timezone, such as America/New_York.") from None
    value = {
        "id": "workspace",
        "name": str(data.get("name", "My workspace"))[:80],
        "timezone": data["timezone"],
    }
    await request.app[KEYS["store"]].put("settings", value)
    return web.json_response(value)


async def export(request):
    store = request.app[KEYS["store"]]
    return web.json_response(
        {
            "version": 2,
            "accounts": [
                clean_account(a) for a in await store.all("accounts") if not a.get("demo")
            ],
            "posts": [
                p
                for p in await store.all("posts")
                if not p.get("demo") and p.get("status") != "simulated"
            ],
            "settings": await store.get("settings", "workspace"),
        },
        headers={"Content-Disposition": 'attachment; filename="lightpost-content.json"'},
    )


async def automation_info(request):
    app = request.app
    automation = app[KEYS["automation"]]
    return web.json_response(
        {
            "enabled": automation["enabled"],
            "locked": automation["locked"],
            "source": automation["source"],
            "key": app[KEYS["config"]].api_key,
        }
    )


async def automation_update(request):
    data = await read_json(request)
    app = request.app
    automation = app[KEYS["automation"]]
    if automation["locked"]:
        raise ValueError("API access is switched off by API_ENABLED=false in .env.")
    if not isinstance(data.get("enabled"), bool):
        raise ValueError("Choose on or off.")
    await app[KEYS["store"]].put("settings", {"id": "automation", "enabled": data["enabled"]})
    automation["enabled"] = data["enabled"]
    return await automation_info(request)


async def automation_regenerate(request):
    app = request.app
    automation = app[KEYS["automation"]]
    if automation["source"] != "file":
        raise ValueError("This key comes from API_KEY in .env. Change it there and restart.")
    # The old key stops working as soon as the new one is saved.
    app[KEYS["config"]].api_key = await asyncio.to_thread(
        save_api_key, automation["directory"], new_api_key()
    )
    return await automation_info(request)


async def static(request):
    root = Path(request.app[KEYS["config"]].web_root).resolve()
    filename = request.match_info.get("path", "")
    if filename in ("", "app", "app/"):
        filename = "index.html"
    path = (root / filename).resolve()
    if (
        not path.is_relative_to(root)
        or not path.is_file()
        or path.suffix not in (".html", ".js", ".css", ".svg", ".png", ".ico")
    ):
        raise web.HTTPNotFound(text="Page not found.")
    return web.FileResponse(path)


async def lifecycle(app):
    config = app[KEYS["config"]]
    store = await Store(config.database).open()
    directory = Path(config.database).parent
    try:
        vault = Vault(config.key, directory)
        transport = app.get(KEYS["test_transport"]) or Transport()
        providers = Providers(vault, transport)
        media_dir = directory / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        jobs = Scheduler(store, providers, media_dir, config.interval)
        # API key: API_KEY in .env wins; otherwise LightPost creates one and keeps it in data/.
        source = "environment" if config.api_key else "file"
        config.api_key = config.api_key or load_api_key(directory)
        switch = await store.get("settings", "automation") or {}
        automation = {
            "directory": directory,
            "source": source,
            "locked": not config.api_enabled,
            "enabled": config.api_enabled and switch.get("enabled", True) is not False,
        }
        from .remote import Remote  # Imported here: remote.py builds on this module.

        remote = Remote(app, say=config.say)
        app.update(
            {
                KEYS["remote"]: remote,
                KEYS["automation"]: automation,
                KEYS["store"]: store,
                KEYS["vault"]: vault,
                KEYS["providers"]: providers,
                KEYS["jobs"]: jobs,
            }
        )
        if config.scheduler:
            await jobs.start()
        else:
            await jobs.recover()
        await remote.apply()
        try:
            yield
        finally:
            await remote.stop()
            await jobs.close()
            if isinstance(transport, Transport):
                await transport.close()
    finally:
        await store.close()


def create_app(config=None, transport=None):
    app = web.Application(middlewares=[guard], client_max_size=2 * 1024 * 1024)
    app.update(
        {KEYS["config"]: config or Config.from_env(), KEYS["sessions"]: {}, KEYS["attempts"]: []}
    )
    if transport:
        app[KEYS["test_transport"]] = transport
    app.cleanup_ctx.append(lifecycle)
    from .api import routes as api_routes  # Imported here: api.py builds on this module.
    from .remote import routes as remote_routes

    app.add_routes(api_routes())
    app.add_routes(remote_routes())
    app.add_routes(
        [
            web.get("/api/session", session),
            web.post("/api/login", login),
            web.post("/api/logout", logout),
            web.get("/api/state", state),
            web.get("/api/posts", list_posts),
            web.post("/api/posts", save_post),
            web.post("/api/posts/{id}/retry", retry),
            web.post("/api/posts/{id}/cancel", cancel),
            web.delete("/api/posts/{id}", delete_post),
            web.post("/api/accounts", connect),
            web.get("/api/accounts/{id}/connection", connection_defaults),
            web.delete("/api/accounts/{id}", disconnect),
            web.post("/api/media", upload),
            web.post("/api/media/url", import_image),
            web.get("/api/media/{id}", media_file),
            web.put("/api/media/{id}", media_alt),
            web.delete("/api/media/{id}", delete_media),
            web.put("/api/settings", settings),
            web.get("/api/export", export),
            web.get("/api/automation", automation_info),
            web.put("/api/automation", automation_update),
            web.post("/api/automation/regenerate", automation_regenerate),
            web.get("/{path:.*}", static),
        ]
    )
    return app
