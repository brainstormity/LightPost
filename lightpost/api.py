"""Automation interface: a REST API (/api/v1) and an MCP server (/mcp).

Both are thin layers over the same functions the browser UI uses (write_post, retry_post, ...), so
every validation rule, platform limit and safety check applies equally. Both are protected by the
API_KEY setting and stay switched off until it is set.
"""

import base64
import binascii
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

from . import __version__
from .app import (
    KEYS,
    cancel_post,
    clean_account,
    image_mime,
    read_json,
    remove_media,
    remove_post,
    retry_post,
    save_image,
    set_media_alt,
    upload,
    write_post,
)
from .providers import ProviderError, image_url

MAX_IMAGE = 1024 * 1024
PLATFORM_ALIASES = {"twitter": "x"}
TIME_HELP = (
    "scheduledAt must be an ISO 8601 date and time such as 2026-11-02T09:30:00+02:00, "
    "or 2026-11-02T09:30:00 together with a timezone."
)

# ---------------------------------------------------------------------------------------------
# Shared operations
# ---------------------------------------------------------------------------------------------


async def live_accounts(app):
    return [a for a in await app[KEYS["store"]].all("accounts") if not a.get("demo")]


def public_account(account):
    account = clean_account(account)
    return {k: account[k] for k in ("id", "platform", "name", "handle", "limit") if k in account}


def public_media(media):
    return {
        "id": media["id"],
        "name": media["name"],
        "mime": media["mime"],
        "size": media["size"],
        "alt": media.get("alt", ""),
        "publicSource": bool(media.get("sourceUrl")),
    }


def describe(account):
    return f"{account['platform']} {account.get('handle') or ''} (id {account['id']})".replace(
        "  ", " "
    )


def pick_accounts(accounts, ref):
    """Match accounts by id, then handle, then platform or name. Returns every match."""
    key = ref.strip().lower()
    key = PLATFORM_ALIASES.get(key, key)
    if key == "all":
        return list(accounts)
    handle = key.lstrip("@")
    tests = (
        lambda a: a["id"].lower() == key,
        lambda a: bool(handle) and str(a.get("handle") or "").lower().lstrip("@") == handle,
        lambda a: key in (a["platform"], str(a.get("name") or "").lower()),
    )
    for test in tests:
        found = [a for a in accounts if test(a)]
        if found:
            return found
    return []


def resolve_accounts(accounts, refs):
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list) or not refs or not all(isinstance(r, str) for r in refs):
        raise ValueError(
            "Choose at least one connected account in accounts: an id, a handle, a platform "
            'name or "all". Use list_accounts (GET /api/v1/accounts) to see them.'
        )
    ids = []
    for ref in refs:
        found = pick_accounts(accounts, ref)
        if not found:
            raise ValueError(
                f"No connected account matches {ref!r}. Connected: "
                + ("; ".join(map(describe, accounts)) or "none")
                + "."
            )
        if len(found) > 1 and ref.strip().lower() != "all":
            raise ValueError(
                f"{ref!r} matches several accounts. Use an id or handle instead: "
                + "; ".join(map(describe, found))
                + "."
            )
        ids += [a["id"] for a in found]
    return list(dict.fromkeys(ids))


async def schedule_time(app, value, zone):
    """Return (UTC ISO time, note). A time without an offset uses `zone` or the workspace zone."""
    if not isinstance(value, str):
        raise ValueError(TIME_HELP)
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(TIME_HELP) from None
    note = None
    if moment.tzinfo is None:
        if zone is not None and not isinstance(zone, str):
            raise ValueError("timezone must be an IANA name such as Asia/Jerusalem.")
        settings = await app[KEYS["store"]].get("settings", "workspace") or {}
        name = zone or settings.get("timezone") or "UTC"
        try:
            moment = moment.replace(tzinfo=ZoneInfo(name))
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(
                f"Unknown timezone {name!r}. Use an IANA name such as Asia/Jerusalem."
            ) from None
        if not zone:
            note = (
                f"scheduledAt had no offset, so the workspace timezone {name} was used. "
                "Include an offset or a timezone to be explicit."
            )
    return moment.astimezone(timezone.utc).isoformat(), note


async def add_image(app, spec):
    """Store one image from a URL, base64 data or an existing library id.

    Returns (media record, True when a new library entry was created).
    """
    if isinstance(spec, str):
        spec = {"url": spec}
    if not isinstance(spec, dict):
        raise ValueError("Each image must be a URL string or an object with url, data or mediaId.")
    alt = spec.get("alt", "")
    if not isinstance(alt, str) or len(alt) > 1000:
        raise ValueError("Alt text must be 1,000 characters or fewer.")
    sources = [k for k in ("url", "data", "mediaId") if spec.get(k)]
    if len(sources) != 1:
        raise ValueError("Give each image exactly one of url, data (base64) or mediaId.")
    store = app[KEYS["store"]]
    name = spec["name"] if isinstance(spec.get("name"), str) else ""
    source_url = None
    if sources[0] == "mediaId":
        media = (
            await store.get("media", spec["mediaId"]) if isinstance(spec["mediaId"], str) else None
        )
        if not media:
            raise ValueError("That image is not in the library. Upload it first.")
        if alt and alt != media.get("alt"):
            await set_media_alt(app, media["id"], alt)
            media = await store.get("media", media["id"])
        return media, False
    if sources[0] == "url":
        url = image_url(spec["url"])
        # The transport validates and pins DNS; redirects and private addresses are blocked.
        try:
            content = await app[KEYS["providers"]].send("GET", url, _binary=True, _limit=MAX_IMAGE)
        except ProviderError as error:
            raise ValueError(
                f"Could not download the image from {url} ({error}). Use a direct public HTTPS "
                "link to a JPEG, PNG or WebP of 1 MB or less, or send the image as base64 data."
            ) from None
        if not isinstance(content, bytes) or len(content) > MAX_IMAGE:
            raise ValueError("Use an image no larger than 1 MB.")
        name = name or Path(urlsplit(url).path).name or "Image"
        if image_mime(content) == "image/jpeg":
            source_url = url
    else:
        raw = spec["data"]
        if not isinstance(raw, str):
            raise ValueError("data must be a base64 string.")
        if raw.startswith("data:"):
            raw = raw.partition(",")[2]
        if len(raw) > MAX_IMAGE * 4 // 3 + 16:
            raise ValueError("Resize images to 1 MB or smaller.")
        try:
            content = base64.b64decode("".join(raw.split()), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("data must be valid base64.") from None
        if len(content) > MAX_IMAGE:
            raise ValueError("Resize images to 1 MB or smaller.")
        name = name or "Image"
    media = await save_image(
        app,
        content,
        image_mime(content),
        name[:150],
        alt=alt,
        **({"sourceUrl": source_url} if source_url else {}),
    )
    return media, True


async def discard_media(app, ids):
    for id in ids:
        try:
            await remove_media(app, id)
        except Exception:
            pass  # An unused library image is harmless; never hide the original error.


POST_FIELDS = (
    "text",
    "accounts",
    "images",
    "status",
    "scheduledAt",
    "timezone",
    "contentOptions",
    "xAllowLinks",
    "xCostConfirmed",
    "idempotencyKey",
)


async def create_post(app, args, post_id=None):
    """Create a post (or update one when post_id is given). Returns (post, HTTP status)."""
    if not isinstance(args, dict):
        raise ValueError("Send a JSON object.")
    unknown = sorted(set(args) - set(POST_FIELDS) - {"accountIds", "mediaIds"})
    if unknown:
        raise ValueError(
            "Unknown field(s): " + ", ".join(unknown) + ". Allowed: " + ", ".join(POST_FIELDS) + "."
        )
    store = app[KEYS["store"]]
    old = await store.get("posts", post_id) if post_id else None
    if post_id and not old:
        raise web.HTTPNotFound(text="Post not found.")
    key = args.get("idempotencyKey")
    if not post_id and isinstance(key, str) and key and (same := await store.post_with_key(key)):
        return same, 200

    accounts = await live_accounts(app)
    refs = args.get("accounts", args.get("accountIds", old["accountIds"] if old else None))
    options = args.get("contentOptions", old.get("contentOptions", {}) if old else {})
    if "contentOptions" in args:
        if not isinstance(options, dict):
            raise ValueError("Choose valid Text and Image options.")
        mapped = {}
        for ref, value in options.items():
            found = pick_accounts(accounts, ref)
            if len(found) != 1:
                raise ValueError(f"contentOptions: {ref!r} must match exactly one account.")
            mapped[found[0]["id"]] = value
        options = mapped
    scheduled, status = args.get("scheduledAt"), args.get("status")
    status = "now" if status == "publish" else status
    if status is None:
        if scheduled:
            status = "scheduled"
        elif old and old["status"] == "scheduled":
            status, scheduled = "scheduled", old["scheduledAt"]
        else:
            status = "draft"
    if status not in ("draft", "scheduled", "now"):
        raise ValueError('status must be "draft", "scheduled" or "now".')
    if scheduled and status != "scheduled":
        raise ValueError('scheduledAt requires status "scheduled" (or leave status out).')

    data = {
        "status": status,
        "text": args.get("text", old["text"] if old else ""),
        "accountIds": resolve_accounts(accounts, refs),
        "contentOptions": options,
        "xAllowLinks": args.get("xAllowLinks", old.get("xAllowLinks", False) if old else False),
        "xCostConfirmed": args.get("xCostConfirmed"),
        "idempotencyKey": key,
    }
    notes = []
    if post_id:
        data["id"] = post_id
    if status == "scheduled":
        data["scheduledAt"], note = await schedule_time(app, scheduled, args.get("timezone"))
        notes += [note] if note else []

    specs = args.get("images", [])
    if isinstance(specs, (str, dict)):
        specs = [specs]
    if not isinstance(specs, list):
        raise ValueError("images must be a list.")
    if not isinstance(args.get("mediaIds", []), list):
        raise ValueError("mediaIds must be a list.")
    # Giving images or mediaIds replaces the attached images; leaving both out keeps them.
    if "images" in args or "mediaIds" in args:
        media_ids = list(args.get("mediaIds", []))
    else:
        media_ids = list(old["mediaIds"]) if old else []
    if len(media_ids) + len(specs) > 4:
        raise ValueError("Choose up to four images.")

    created = []
    try:
        for spec in specs:
            media, is_new = await add_image(app, spec)
            created += [media["id"]] if is_new else []
            media_ids.append(media["id"])
        data["mediaIds"] = media_ids
        post, code = await write_post(app, data)
    except BaseException:
        await discard_media(app, created)
        raise
    if code == 200:  # Lost a race with an identical idempotencyKey; keep the original post.
        await discard_media(app, created)
    return ({**post, "notes": notes} if notes else post), code


async def list_posts(app, status="", offset=0):
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        raise ValueError("Invalid page.") from None
    if not isinstance(status, str):
        raise ValueError("Invalid status.")
    posts, total = await app[KEYS["store"]].posts(status, offset)
    return {"posts": posts, "total": total, "offset": offset, "pageSize": 50}


async def get_post(app, id):
    post = await app[KEYS["store"]].get("posts", id) if isinstance(id, str) else None
    if not post or post.get("demo") or post.get("status") == "simulated":
        raise web.HTTPNotFound(text="Post not found.")
    return post


# ---------------------------------------------------------------------------------------------
# Schemas shared by the OpenAPI document and the MCP tool definitions
# ---------------------------------------------------------------------------------------------

IMAGE_OBJECT = {
    "type": "object",
    "description": "Give exactly one of url, data or mediaId.",
    "properties": {
        "url": {"type": "string", "description": "Direct public HTTPS image URL."},
        "data": {"type": "string", "description": "Base64-encoded image bytes (data: URIs work)."},
        "mediaId": {"type": "string", "description": "An image already in the library."},
        "alt": {"type": "string", "maxLength": 1000, "description": "Alt text for accessibility."},
        "name": {"type": "string", "description": "Optional file name."},
    },
    "additionalProperties": False,
}

POST_PROPERTIES = {
    "text": {
        "type": "string",
        "maxLength": 10000,
        "description": "Post text. Platform limits apply (for example, X allows 280 characters).",
    },
    "accounts": {
        "type": "array",
        "items": {"type": "string"},
        "description": 'Where to post: account ids, handles (like "@name"), platform names ("x", '
        '"linkedin", "bluesky") or "all". A platform name must match exactly one account.',
    },
    "images": {
        "type": "array",
        "maxItems": 4,
        "items": IMAGE_OBJECT,
        "description": "Up to four JPEG, PNG or WebP images of 1 MB or less. Instagram and "
        "Threads need a single public JPEG url.",
    },
    "status": {
        "type": "string",
        "enum": ["draft", "scheduled", "now"],
        "description": "draft saves without publishing (the default when scheduledAt is absent). "
        "scheduled publishes at scheduledAt (the default when scheduledAt is given). now "
        "publishes immediately and publicly.",
    },
    "scheduledAt": {
        "type": "string",
        "description": "Future ISO 8601 date and time, e.g. 2026-11-02T09:30:00+02:00. Without "
        "an offset, timezone (or the workspace timezone, default UTC) is applied.",
    },
    "timezone": {
        "type": "string",
        "description": "IANA timezone such as Asia/Jerusalem, for a scheduledAt without an offset.",
    },
    "contentOptions": {
        "type": "object",
        "description": "Optional per-account switches keyed by account id, handle or platform, "
        'e.g. {"x": {"image": false}}.',
        "additionalProperties": {
            "type": "object",
            "properties": {"text": {"type": "boolean"}, "image": {"type": "boolean"}},
        },
    },
    "xAllowLinks": {"type": "boolean", "description": "Allow links in text posted to X."},
    "xCostConfirmed": {
        "type": "boolean",
        "description": "Required to publish or schedule to X, which bills API usage. Set it only "
        "after the user has accepted the cost.",
    },
    "idempotencyKey": {
        "type": "string",
        "maxLength": 200,
        "description": "Optional unique key. Repeating a request with the same key returns the "
        "original post instead of creating another.",
    },
}

INSTRUCTIONS = (
    "LightPost publishes to the owner's connected social accounts. Call list_accounts first. "
    'create_post saves a draft unless you pass scheduledAt or status "now"; publishing is public '
    "and cannot be undone, so only publish or schedule when the user asked for it. Posting to X "
    "needs xCostConfirmed after the user accepts the API cost. Use idempotencyKey when a request "
    "might be repeated."
)


def tool(name, description, properties=None, required=(), **annotations):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
        "annotations": annotations,
    }


POST_ID = {"postId": {"type": "string", "description": "The post id."}}
TOOLS = [
    tool(
        "list_accounts",
        "List the connected social accounts (id, platform, handle) that posts can go to.",
        readOnlyHint=True,
    ),
    tool(
        "create_post",
        "Create a post with text and optional images, as a draft, scheduled, or published now. "
        "Pass postId to change an existing draft or scheduled post; fields you leave out are kept.",
        {**POST_PROPERTIES, **POST_ID},
        required=("accounts",),
        destructiveHint=False,
    ),
    tool(
        "list_posts",
        "List posts, newest first (scheduled posts soonest first), 50 per page.",
        {
            "status": {
                "type": "string",
                "description": "Optional filter: draft, scheduled, queued, publishing, published, "
                "partial, failed, needs_review, blocked, cancelled or attention.",
            },
            "offset": {"type": "integer", "minimum": 0, "description": "Skip this many posts."},
        },
        readOnlyHint=True,
    ),
    tool(
        "get_post",
        "Get one post with its per-account delivery results.",
        POST_ID,
        required=("postId",),
        readOnlyHint=True,
    ),
    tool(
        "cancel_post",
        "Stop a scheduled or queued post. A scheduled post returns to draft; one already "
        "publishing is stopped where possible.",
        POST_ID,
        required=("postId",),
    ),
    tool(
        "retry_post",
        "Retry the destinations of a failed or partial post. Already published ones are skipped.",
        {**POST_ID, "xCostConfirmed": POST_PROPERTIES["xCostConfirmed"]},
        required=("postId",),
    ),
    tool(
        "delete_post",
        "Delete a post record from LightPost. Does not remove anything already published.",
        POST_ID,
        required=("postId",),
        destructiveHint=True,
    ),
    tool(
        "upload_image",
        "Add an image to the library and get a mediaId to reuse. Optional: create_post accepts "
        "images directly.",
        {k: v for k, v in IMAGE_OBJECT["properties"].items() if k != "mediaId"},
    ),
]


def post_id(args):
    value = args.get("postId")
    if not isinstance(value, str) or not value:
        raise ValueError("postId is required.")
    return value


async def _list_accounts(app, args):
    return {"accounts": [public_account(a) for a in await live_accounts(app)]}


async def _create_post(app, args):
    existing = args.get("postId")
    if existing is not None and (not isinstance(existing, str) or not existing):
        raise ValueError("postId must be a post id.")
    post, _ = await create_post(app, {k: v for k, v in args.items() if k != "postId"}, existing)
    return post


async def _list_posts(app, args):
    return await list_posts(app, args.get("status", ""), args.get("offset", 0))


async def _get_post(app, args):
    return await get_post(app, post_id(args))


async def _cancel_post(app, args):
    return await cancel_post(app, post_id(args))


async def _retry_post(app, args):
    return await retry_post(app, post_id(args), {"xCostConfirmed": args.get("xCostConfirmed")})


async def _delete_post(app, args):
    await remove_post(app, post_id(args))
    return {"ok": True}


async def _upload_image(app, args):
    media, _ = await add_image(app, args)
    return public_media(media)


TOOL_IMPLS = {
    "list_accounts": _list_accounts,
    "create_post": _create_post,
    "list_posts": _list_posts,
    "get_post": _get_post,
    "cancel_post": _cancel_post,
    "retry_post": _retry_post,
    "delete_post": _delete_post,
    "upload_image": _upload_image,
}

# ---------------------------------------------------------------------------------------------
# MCP (Streamable HTTP transport, JSON responses, stateless)
# ---------------------------------------------------------------------------------------------

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def rpc_error(id, code, message):
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def tool_result(text, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error}


async def call_tool(app, params):
    name = params.get("name") if isinstance(params, dict) else None
    implementation = TOOL_IMPLS.get(name)
    if not implementation:
        raise RpcError(-32602, f"Unknown tool: {name}")
    args = params.get("arguments")
    args = {} if args is None else args
    if not isinstance(args, dict):
        raise RpcError(-32602, "arguments must be an object.")
    try:
        result = await implementation(app, args)
        return tool_result(json.dumps(result, indent=2, ensure_ascii=False))
    except web.HTTPException as error:
        return tool_result(error.text or error.reason, True)
    except (ValueError, ProviderError) as error:
        return tool_result(str(error), True)
    except Exception:
        # Never log provider payloads or credentials.
        logging.getLogger("lightpost").error("MCP tool %s failed unexpectedly.", name)
        return tool_result("Unable to complete this request. No changes were confirmed.", True)


async def handle_rpc(app, message):
    """Return a JSON-RPC response, or None for notifications and client responses."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return rpc_error(None, -32600, "Invalid JSON-RPC request.")
    method, id = message.get("method"), message.get("id")
    if method is None:
        return (
            None
            if "result" in message or "error" in message
            else rpc_error(id, -32600, "Invalid JSON-RPC request.")
        )
    notification = "id" not in message
    try:
        if method == "initialize":
            params = message.get("params")
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            result = {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "lightpost", "title": "LightPost", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = await call_tool(app, message.get("params"))
        elif notification:
            return None  # notifications/initialized, notifications/cancelled, ...
        else:
            raise RpcError(-32601, f"Method not found: {method}")
    except RpcError as error:
        return None if notification else rpc_error(id, error.code, str(error))
    return None if notification else {"jsonrpc": "2.0", "id": id, "result": result}


async def mcp(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        return web.json_response(rpc_error(None, -32700, "Parse error."), status=400)
    if isinstance(body, list):
        if not body:
            return web.json_response(rpc_error(None, -32600, "Empty batch."), status=400)
        replies = [r for r in [await handle_rpc(request.app, m) for m in body] if r is not None]
        return web.json_response(replies) if replies else web.Response(status=202)
    reply = await handle_rpc(request.app, body)
    return web.json_response(reply) if reply is not None else web.Response(status=202)


async def mcp_get(request):
    return web.json_response(
        {"error": "This MCP server accepts POST (Streamable HTTP with JSON responses)."},
        status=405,
        headers={"Allow": "POST"},
    )


# ---------------------------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------------------------


async def optional_json(request):
    return await read_json(request) if request.can_read_body else {}


async def rest_accounts(request):
    return web.json_response(await _list_accounts(request.app, {}))


async def rest_posts(request):
    return web.json_response(
        await list_posts(
            request.app, request.query.get("status", ""), request.query.get("offset", 0)
        )
    )


async def rest_post_create(request):
    post, code = await create_post(request.app, await read_json(request))
    return web.json_response(post, status=code)


async def rest_post_get(request):
    return web.json_response(await get_post(request.app, request.match_info["id"]))


async def rest_post_update(request):
    post, code = await create_post(request.app, await read_json(request), request.match_info["id"])
    return web.json_response(post, status=code)


async def rest_post_cancel(request):
    return web.json_response(await cancel_post(request.app, request.match_info["id"]))


async def rest_post_retry(request):
    post = await retry_post(request.app, request.match_info["id"], await optional_json(request))
    return web.json_response(post, status=202)


async def rest_post_delete(request):
    await remove_post(request.app, request.match_info["id"])
    return web.json_response({"ok": True})


async def rest_media_list(request):
    media = sorted(await request.app[KEYS["store"]].all("media"), key=lambda m: m["createdAt"])
    return web.json_response({"images": [public_media(m) for m in reversed(media)]})


async def rest_media_create(request):
    app = request.app
    if request.content_type.startswith("multipart/"):
        # Same checks as the browser upload: JPEG, PNG or WebP, 1 MB at most.
        media = json.loads((await upload(request)).body)
        alt = request.query.get("alt", "")
        if alt:
            await set_media_alt(app, media["id"], alt)
            media["alt"] = alt
    else:
        media, _ = await add_image(app, await read_json(request))
    return web.json_response(public_media(media), status=201)


async def rest_media_delete(request):
    await remove_media(request.app, request.match_info["id"])
    return web.json_response({"ok": True})


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def json_body(schema):
    return {"content": {"application/json": {"schema": schema}}}


def openapi_document(request):
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    path_id = [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]
    ok = {"200": {"description": "OK", "content": {"application/json": {"schema": ref("Post")}}}}
    created = {
        "201": {"description": "Saved", "content": {"application/json": {"schema": ref("Post")}}},
        "202": {"description": "Queued", "content": {"application/json": {"schema": ref("Post")}}},
        "200": {"description": "Existing post (same idempotencyKey)"},
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "LightPost API",
            "version": __version__,
            "description": "Create, schedule and manage social posts. Send the API_KEY as "
            "'Authorization: Bearer <key>' (or an X-API-Key header).",
        },
        "servers": [{"url": f"{scheme}://{request.host}"}],
        "security": [{"bearer": []}],
        "paths": {
            "/api/v1/accounts": {
                "get": {
                    "operationId": "listAccounts",
                    "summary": "List connected accounts",
                    "responses": {"200": {"description": "Accounts"}},
                }
            },
            "/api/v1/posts": {
                "get": {
                    "operationId": "listPosts",
                    "summary": "List posts",
                    "parameters": [
                        {"name": "status", "in": "query", "schema": {"type": "string"}},
                        {"name": "offset", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "Posts"}},
                },
                "post": {
                    "operationId": "createPost",
                    "summary": "Create a draft, scheduled or immediate post",
                    "requestBody": {"required": True, **json_body(ref("NewPost"))},
                    "responses": created,
                },
            },
            "/api/v1/posts/{id}": {
                "get": {
                    "operationId": "getPost",
                    "summary": "Get a post",
                    "parameters": path_id,
                    "responses": ok,
                },
                "put": {
                    "operationId": "updatePost",
                    "summary": "Change a draft or scheduled post (omitted fields are kept)",
                    "parameters": path_id,
                    "requestBody": {"required": True, **json_body(ref("NewPost"))},
                    "responses": created,
                },
                "delete": {
                    "operationId": "deletePost",
                    "summary": "Delete a post record",
                    "parameters": path_id,
                    "responses": {"200": {"description": "Deleted"}},
                },
            },
            "/api/v1/posts/{id}/cancel": {
                "post": {
                    "operationId": "cancelPost",
                    "summary": "Cancel a scheduled or active post",
                    "parameters": path_id,
                    "responses": ok,
                }
            },
            "/api/v1/posts/{id}/retry": {
                "post": {
                    "operationId": "retryPost",
                    "summary": "Retry failed destinations",
                    "parameters": path_id,
                    "requestBody": json_body(
                        {"type": "object", "properties": {"xCostConfirmed": {"type": "boolean"}}}
                    ),
                    "responses": {"202": {"description": "Queued"}},
                }
            },
            "/api/v1/media": {
                "get": {
                    "operationId": "listImages",
                    "summary": "List library images",
                    "responses": {"200": {"description": "Images"}},
                },
                "post": {
                    "operationId": "uploadImage",
                    "summary": "Add an image by URL, base64 or multipart upload (field: file)",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": IMAGE_OBJECT},
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"file": {"type": "string", "format": "binary"}},
                                }
                            },
                        },
                    },
                    "responses": {"201": {"description": "Image added"}},
                },
            },
            "/api/v1/media/{id}": {
                "delete": {
                    "operationId": "deleteImage",
                    "summary": "Delete a library image",
                    "parameters": path_id,
                    "responses": {"200": {"description": "Deleted"}},
                }
            },
        },
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "NewPost": {
                    "type": "object",
                    "required": ["accounts"],
                    "properties": POST_PROPERTIES,
                    "additionalProperties": False,
                },
                "Post": {"type": "object", "description": "A saved post with delivery results."},
            },
        },
    }


async def rest_openapi(request):
    return web.json_response(openapi_document(request))


def routes():
    return [
        web.post("/mcp", mcp),
        web.post("/mcp/", mcp),
        web.get("/mcp", mcp_get),
        web.get("/mcp/", mcp_get),
        web.post("/mcp/{token}", mcp),
        web.get("/mcp/{token}", mcp_get),
        web.post("/mcp/{token}/", mcp),
        web.get("/mcp/{token}/", mcp_get),
        web.get("/api/v1/openapi.json", rest_openapi),
        web.get("/api/v1/accounts", rest_accounts),
        web.get("/api/v1/posts", rest_posts),
        web.post("/api/v1/posts", rest_post_create),
        web.get("/api/v1/posts/{id}", rest_post_get),
        web.put("/api/v1/posts/{id}", rest_post_update),
        web.delete("/api/v1/posts/{id}", rest_post_delete),
        web.post("/api/v1/posts/{id}/cancel", rest_post_cancel),
        web.post("/api/v1/posts/{id}/retry", rest_post_retry),
        web.get("/api/v1/media", rest_media_list),
        web.post("/api/v1/media", rest_media_create),
        web.delete("/api/v1/media/{id}", rest_media_delete),
    ]
