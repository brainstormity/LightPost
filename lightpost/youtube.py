"""Personal YouTube connections, bounded local video storage and resumable uploads."""

import asyncio
import base64
import hashlib
import hmac
import math
import re
import secrets
import time
from html import escape
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from aiohttp import ClientTimeout, web

from .providers import ProviderError, now

MAX_VIDEO = 250 * 1024 * 1024
CHUNK_SIZE = 8 * 1024 * 1024
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTubeReconnectRequired(ProviderError):
    reconnect_required = True

    def __init__(self):
        super().__init__(
            "Your YouTube authorization expired or was revoked. Reconnect your account with Google, then try again."
        )
        self.reconnect_account_ids = []


def client_fields(data):
    fields = {}
    for name in ("clientId", "clientSecret"):
        value = data.get(name)
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2048:
            raise ValueError("Enter your Google OAuth client ID and client secret.")
        fields[name] = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.apps\.googleusercontent\.com", fields["clientId"]):
        raise ValueError("Use a Google OAuth client ID ending in .apps.googleusercontent.com.")
    return fields


async def token_request(providers, fields):
    try:
        result = await providers.send("POST", TOKEN_URL, data=fields)
    except ProviderError as error:
        if error.status in (400, 401, 403):
            raise YouTubeReconnectRequired() from None
        raise ProviderError(
            "Unable to reach Google to check YouTube authorization. Try again shortly; your connection may still be valid."
        ) from None
    if not isinstance(result.get("access_token"), str) or not result["access_token"]:
        raise ProviderError("Google did not return an access token. Reconnect YouTube.")
    return result


async def refresh(providers, credentials):
    stamp = time.time()
    expiry = credentials.get("tokenExpiresAt")
    if (
        isinstance(credentials.get("token"), str)
        and credentials["token"]
        and type(expiry) in (int, float)
        and math.isfinite(expiry)
        and expiry > stamp + 60
    ):
        return credentials
    result = await token_request(
        providers,
        {
            "grant_type": "refresh_token",
            "client_id": credentials["clientId"],
            "client_secret": credentials["clientSecret"],
            "refresh_token": credentials["refreshToken"],
        },
    )
    try:
        lifetime = float(result.get("expires_in", 0))
    except (ValueError, TypeError, OverflowError):
        lifetime = 0
    if not math.isfinite(lifetime) or lifetime < 0:
        lifetime = 0
    return {
        **credentials,
        "token": result["access_token"],
        "tokenExpiresAt": stamp + lifetime,
        **(
            {"refreshToken": result["refresh_token"]}
            if isinstance(result.get("refresh_token"), str) and result["refresh_token"]
            else {}
        ),
    }


async def check_accounts(app, identities):
    """Preflight delivery authorization. Drafts never need an online check."""
    key = keys()
    store, jobs, providers = (app[key[name]] for name in ("store", "jobs", "providers"))
    for identity in identities:
        account = await store.get("accounts", identity)
        if not account or account["platform"] != "youtube" or account.get("demo"):
            continue
        async with jobs.account_locks.setdefault(identity, asyncio.Lock()):
            credentials = providers.vault.decrypt(account["credentials"])
            try:
                updated = await refresh(providers, credentials)
            except YouTubeReconnectRequired as error:
                await store.put(
                    "accounts",
                    {
                        **account,
                        "connectionStatus": "reconnect_required",
                        "connectionMessage": str(error),
                    },
                )
                error.reconnect_account_ids = [identity]
                raise
            if updated != credentials or account.get("connectionStatus") == "reconnect_required":
                await store.put(
                    "accounts",
                    {
                        **account,
                        "credentials": providers.vault.encrypt(updated),
                        "connectionStatus": "connected",
                        "connectionMessage": "",
                    },
                )


async def connect_channel(providers, data):
    credentials = client_fields(data)
    secret = data.get("secret")
    if not isinstance(secret, str) or not 1 <= len(secret.strip()) <= 8192:
        raise ValueError("Connect with your downloaded Google JSON, or enter a refresh token.")
    credentials["refreshToken"] = secret.strip()
    credentials = await refresh(providers, credentials)
    result = await providers.send(
        "GET",
        API + "/channels",
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": "Bearer " + credentials["token"]},
    )
    channels = result.get("items", [])
    if len(channels) != 1 or not re.fullmatch(
        r"UC[A-Za-z0-9_-]{22}", str(channels[0].get("id", ""))
    ):
        raise ValueError(
            "Authorize a Google account with a YouTube channel. Choose the intended channel during Google sign-in."
        )
    channel = channels[0]
    credentials["channelId"] = channel["id"]
    return {
        "platform": "youtube",
        "name": "YouTube",
        "remoteId": channel["id"],
        "handle": str(channel.get("snippet", {}).get("title") or channel["id"])[:150],
        "demo": False,
        "credentials": providers.vault.encrypt(credentials),
    }


def validate_video_post(post):
    details = post.get("youtube")
    if not isinstance(details, dict):
        raise ValueError("Enter YouTube video details.")
    title = details.get("title")
    if (
        not isinstance(title, str)
        or not title.strip()
        or len(title) > 100
        or any(c in title for c in "<>")
    ):
        raise ValueError("YouTube needs a title of 1–100 characters without < or >.")
    if len(post.get("text", "").encode("utf-8")) > 5000 or any(
        c in post.get("text", "") for c in "<>"
    ):
        raise ValueError(
            "YouTube descriptions must fit 5,000 UTF-8 bytes and cannot contain < or >."
        )
    if details.get("visibility") not in ("private", "unlisted", "public"):
        raise ValueError("Choose YouTube visibility: Private, Unlisted or Public.")
    if (
        type(details.get("madeForKids")) is not bool
        or type(details.get("syntheticMedia")) is not bool
    ):
        raise ValueError("Choose the YouTube audience and altered/synthetic content disclosure.")
    if not post.get("videoId") or post.get("mediaIds"):
        raise ValueError("YouTube needs one MP4 video and no image attachments.")


def session_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.googleapis.com"
        or parsed.path != "/upload/youtube/v3/videos"
        or not parsed.query
        or parsed.fragment
        or len(value) > 8192
    ):
        raise ProviderError("YouTube returned an invalid upload session.")
    return value


async def publish_video(
    providers, account, post, credentials, save_credentials, previous, checkpoint
):
    refreshed = await refresh(providers, credentials)
    if refreshed.get("channelId") != account["remoteId"]:
        raise ProviderError("YouTube channel identity changed. Reconnect this account.")
    if refreshed != credentials:
        await save_credentials(refreshed)
    credentials = refreshed
    headers = {"Authorization": "Bearer " + credentials["token"]}
    previous = previous or {}
    if previous.get("stage") == "verifying" and previous.get("containerId"):
        video_id = previous["containerId"]
        result = await providers.send(
            "GET",
            API + "/videos",
            params={"part": "status,processingDetails", "id": video_id},
            headers=headers,
        )
        items = result.get("items", [])
        if not items:
            return {
                **previous,
                "status": "failed",
                "message": "Uploaded video is unavailable. Check YouTube Studio before retrying.",
            }
        status = items[0].get("status", {})
        processing = items[0].get("processingDetails", {}).get("processingStatus")
        if status.get("uploadStatus") in ("failed", "rejected", "deleted") or processing in (
            "failed",
            "terminated",
        ):
            return {
                **previous,
                "status": "failed",
                "message": "YouTube could not process this video. Check YouTube Studio; retry will check this same video.",
            }
        if status.get("uploadStatus") == "processed" or processing == "succeeded":
            visibility = status.get("privacyStatus")
            if visibility != post["youtube"]["visibility"]:
                return {
                    **previous,
                    "status": "failed",
                    "message": "YouTube visibility differs from your choice. Check YouTube Studio for restrictions; the video will not be re-uploaded automatically.",
                }
            return {
                "status": "published",
                "remoteId": video_id,
                "url": "https://www.youtube.com/watch?v=" + video_id,
                "message": "Uploaded to YouTube as " + visibility + ".",
            }
        if time.time() - previous.get("verificationStartedAt", time.time()) > 3600:
            return {
                **previous,
                "status": "uncertain",
                "message": "YouTube is still processing after an hour. Check YouTube Studio before retrying.",
            }
        return {
            **previous,
            "status": "processing",
            "message": "YouTube is processing the uploaded video.",
        }

    video = post["_video"]
    encrypted = previous.get("containerId") if previous.get("stage") == "youtube-upload" else None
    if not encrypted:
        response = await providers.send(
            "POST",
            UPLOAD_URL,
            params={
                "uploadType": "resumable",
                "part": "snippet,status",
                "notifySubscribers": "false",
            },
            headers={
                **headers,
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(video["size"]),
            },
            json={
                "snippet": {
                    "title": post["youtube"]["title"].strip(),
                    "description": post["text"],
                    "categoryId": "22",
                },
                "status": {
                    "privacyStatus": post["youtube"]["visibility"],
                    "selfDeclaredMadeForKids": post["youtube"]["madeForKids"],
                    "containsSyntheticMedia": post["youtube"]["syntheticMedia"],
                },
            },
            _upload=True,
        )
        encrypted = providers.vault.encrypt(
            {"url": session_url(response["headers"].get("Location", ""))}
        )
    state = {
        "status": "processing",
        "stage": "youtube-upload",
        "retryAfter": 30,
        "containerId": encrypted,
        "message": "Uploading video to YouTube.",
    }
    await checkpoint(state)
    url = session_url(providers.vault.decrypt(encrypted)["url"])
    try:
        response = await providers.send(
            "PUT",
            url,
            data=b"",
            headers={**headers, "Content-Length": "0", "Content-Range": f"bytes */{video['size']}"},
            _upload=True,
        )
        while response["status"] == 308:
            received = response["headers"].get("Range", "")
            match = re.fullmatch(r"bytes=0-(\d+)", received) if received else None
            if received and not match:
                raise ProviderError("YouTube returned an invalid upload offset.", uncertain=True)
            offset = int(match[1]) + 1 if match else 0
            if not 0 <= offset < video["size"]:
                raise ProviderError("YouTube returned an invalid upload offset.", uncertain=True)

            def read_chunk():
                with Path(video["path"]).open("rb") as file:
                    file.seek(offset)
                    return file.read(min(CHUNK_SIZE, video["size"] - offset))

            chunk = await asyncio.to_thread(read_chunk)
            if not chunk:
                raise ValueError("The saved video file is incomplete. Upload it again.")
            await checkpoint(
                {
                    **state,
                    "message": f"Uploading video to YouTube ({offset * 100 // video['size']}%).",
                }
            )
            response = await providers.send(
                "PUT",
                url,
                data=chunk,
                headers={
                    **headers,
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{video['size']}",
                },
                timeout=ClientTimeout(total=180),
                _upload=True,
            )
            if response["status"] == 308 and response["headers"].get("Range", "") == received:
                return {
                    **state,
                    "message": "YouTube has not advanced the upload; it will be checked again.",
                }
    except ProviderError as error:
        if error.status in (404, 410):
            return {
                **state,
                "status": "uncertain",
                "message": "YouTube upload session expired. Check YouTube Studio before retrying; a new upload will not be started automatically.",
            }
        if error.uncertain or error.status == 429:
            return {
                **state,
                "message": "YouTube upload interrupted; the saved session will be checked again.",
            }
        raise
    video_id = response["data"].get("id")
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return {
            **state,
            "status": "uncertain",
            "message": "YouTube did not return a video ID. Check YouTube Studio before retrying.",
        }
    state = {
        "status": "processing",
        "stage": "verifying",
        "retryAfter": 15,
        "containerId": video_id,
        "remoteId": video_id,
        "verificationStartedAt": time.time(),
        "url": "https://www.youtube.com/watch?v=" + video_id,
        "message": "YouTube is processing the uploaded video.",
    }
    await checkpoint(state)
    return state


def keys():
    from .app import KEYS

    return KEYS


async def oauth_start(request):
    from .app import read_json

    app, key = request.app, keys()
    if request.scheme != "http" or urlsplit("http://" + request.host).hostname not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        raise ValueError(
            "Google Desktop sign-in needs LightPost opened on this computer over http://127.0.0.1. For a remote server, connect manually with a refresh token."
        )
    data = await read_json(request)
    config = data.get("clientConfig", {})
    installed = config.get("installed", {}) if isinstance(config, dict) else {}
    fields = client_fields(
        {"clientId": installed.get("client_id"), "clientSecret": installed.get("client_secret")}
    )
    existing_id = data.get("id") or None
    if existing_id:
        existing = await app[key["store"]].get("accounts", existing_id)
        if not existing or existing["platform"] != "youtube":
            raise ValueError("Choose an existing YouTube account to reconnect.")
    pending = app[key["youtube_oauth"]]
    for state in list(pending):
        if pending[state]["expires"] < time.monotonic():
            del pending[state]
    if len(pending) >= 20:
        raise ValueError("Too many pending connections. Wait ten minutes and try again.")
    state, verifier, browser = (secrets.token_urlsafe(32) for _ in range(3))
    redirect = f"http://{request.host}/oauth/youtube/callback"
    pending[state] = {
        "fields": fields,
        "verifier": verifier,
        "browser": browser,
        "redirect": redirect,
        "expires": time.monotonic() + 600,
        "id": existing_id,
        "session": request.cookies.get("lightpost_session"),
    }
    query = {
        "client_id": fields["clientId"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("="),
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    response = web.json_response(
        {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(query)}
    )
    response.set_cookie(
        "lightpost_youtube_oauth",
        browser,
        httponly=True,
        samesite="Lax",
        path="/oauth/youtube/callback",
        max_age=600,
    )
    return response


async def oauth_callback(request):
    from .app import persist_account

    app, key = request.app, keys()
    pending = app[key["youtube_oauth"]]
    state = request.query.get("state", "")
    entry = pending.get(state)
    browser = request.cookies.get("lightpost_youtube_oauth", "")
    if (
        not entry
        or not browser
        or not hmac.compare_digest(browser, entry["browser"])
        or entry["expires"] < time.monotonic()
        or entry["redirect"] != f"{request.scheme}://{request.host}{request.path}"
    ):
        raise ValueError(
            "This Google connection expired or belongs to another browser. Start again from Accounts."
        )
    if (
        app[key["config"]].password
        and app[key["sessions"]].get(entry["session"], 0) <= time.monotonic()
    ):
        raise ValueError("Your workspace session expired. Sign in and connect again.")
    del pending[state]
    if request.query.get("error") or not request.query.get("code"):
        raise ValueError("Google authorization was cancelled. Return to Accounts to try again.")
    providers = app[key["providers"]]
    fields = entry["fields"]
    token = await token_request(
        providers,
        {
            "grant_type": "authorization_code",
            "client_id": fields["clientId"],
            "client_secret": fields["clientSecret"],
            "code": request.query["code"],
            "redirect_uri": entry["redirect"],
            "code_verifier": entry["verifier"],
        },
    )
    granted = set(token.get("scope", "").split())
    if not set(SCOPES.split()) <= granted:
        raise ValueError("Grant both YouTube upload and read permissions, then connect again.")
    account = await connect_channel(providers, {**fields, "secret": token.get("refresh_token")})
    await persist_account(app, account, entry["id"])
    response = web.Response(
        text=f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>YouTube connected</title><link rel="stylesheet" href="/styles.css"><main class="signin"><h1>YouTube connected</h1><p>{escape(account["handle"])} is ready. Your credentials are stored encrypted on this server.</p><a class="button primary" href="/app#accounts">Return to Accounts</a></main></html>',
        content_type="text/html",
    )
    response.del_cookie("lightpost_youtube_oauth", path="/oauth/youtube/callback")
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def upload_video(request):
    app, key = request.app, keys()
    try:
        reader = await request.multipart()
        part = await reader.next()
    except (ValueError, AssertionError):
        raise ValueError("Choose an MP4 video file.") from None
    if not part or part.name != "file" or not str(part.filename or "").lower().endswith(".mp4"):
        raise ValueError("Choose an MP4 video file.")
    identity = str(uuid4())
    path = app[key["jobs"]].media_dir / (identity + ".mp4")
    size, header = 0, b""
    try:
        with path.open("xb") as file:
            path.chmod(0o600)
            while chunk := await part.read_chunk(256 * 1024):
                size += len(chunk)
                if size > MAX_VIDEO:
                    raise ValueError("Videos must be 250 MB or smaller.")
                header = (header + chunk)[:32]
                await asyncio.to_thread(file.write, chunk)
        if (
            size < 16
            or header[4:8] != b"ftyp"
            or header[8:12]
            not in (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash", b"MSNV")
        ):
            raise ValueError("Use an MP4 video (H.264 video and AAC audio recommended).")
        video = {
            "id": identity,
            "name": Path(part.filename).name[:150],
            "size": size,
            "mime": "video/mp4",
            "createdAt": now(),
        }
        await app[key["store"]].put("videos", video)
        return web.json_response(video, status=201)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def video_file(request):
    app, key = request.app, keys()
    video = await app[key["jobs"]].video(request.match_info["id"])
    return web.FileResponse(
        video["path"], headers={"Content-Type": "video/mp4", "Cache-Control": "no-store"}
    )


async def delete_video(request):
    app, key = request.app, keys()
    identity = request.match_info["id"]
    async with app[key["jobs"]].post_lock:
        if any(post.get("videoId") == identity for post in await app[key["store"]].all("posts")):
            raise ValueError("Remove this video from saved posts first.")
        video = await app[key["store"]].get("videos", identity)
        if video:
            (app[key["jobs"]].media_dir / (video["id"] + ".mp4")).unlink(missing_ok=True)
            await app[key["store"]].remove("videos", identity)
    return web.json_response({"ok": True})


def routes():
    return [
        web.post("/api/youtube/start", oauth_start),
        web.get("/oauth/youtube/callback", oauth_callback),
        web.post("/api/videos", upload_video),
        web.get("/api/videos/{id}", video_file),
        web.delete("/api/videos/{id}", delete_video),
    ]
