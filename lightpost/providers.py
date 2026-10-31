"""Live adapters for the deliberately small v1 platform set."""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
import unicodedata
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import aiohttp
import regex
from aiohttp.resolver import DefaultResolver

PLATFORMS = [
    {
        "id": "youtube",
        "name": "YouTube",
        "content": "One MP4 video + title and description",
        "limit": 5000,
        "help": "Enable YouTube Data API v3 in your own Google Cloud project. Create a Desktop app OAuth client and download its JSON. Add your Gmail as a test user, then choose the JSON below and connect with Google. Testing-mode authorization normally expires after seven days.",
        "docs": "https://developers.google.com/youtube/v3/guides/uploading_a_video",
    },
    {
        "id": "discord",
        "name": "Discord",
        "content": "Text + up to 4 images",
        "limit": 2000,
        "help": "In your Discord server: Server Settings → Integrations → Webhooks. Create a webhook for a text channel and paste its URL. No bot or developer app approval is needed. Mentions are disabled.",
        "docs": "https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks",
    },
    {
        "id": "telegram",
        "name": "Telegram",
        "content": "Channel text + up to 4 images",
        "limit": 4096,
        "help": "Create a bot with @BotFather using /newbot. Add it to your Telegram channel as an administrator with Post Messages enabled. Enter its bot token and the channel’s @username or numeric -100… ID. Connecting checks permissions without posting a test message.",
        "docs": "https://core.telegram.org/bots/tutorial",
    },
    {
        "id": "facebook",
        "name": "Facebook Pages",
        "content": "Page text + up to 4 images",
        "limit": 10000,
        "help": "Enter your Facebook Page ID and a Page access token for that same Page. In your Meta app, enable the Pages use case and grant pages_manage_posts, pages_read_engagement and pages_show_list. A user token or Instagram token cannot replace the Page token. Connecting verifies the Page without publishing a test post.",
        "docs": "https://developers.facebook.com/docs/pages-api/getting-started/",
    },
    {
        "id": "instagram",
        "name": "Instagram",
        "content": "One public JPEG + caption",
        "limit": 2200,
        "help": "Requires an Instagram Creator or Business account and an Instagram Login long-lived token with instagram_business_basic and instagram_business_content_publish. Personal accounts cannot use the publishing API. Add your own account to your Meta app and authorize the publishing permissions.",
        "docs": "https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/",
    },
    {
        "id": "threads",
        "name": "Threads",
        "content": "Text or one public JPEG",
        "limit": 500,
        "help": "Use your own Threads account and a long-lived Threads API token with threads_basic and threads_content_publish. Add and authorize your account as a tester in your Meta app.",
        "docs": "https://developers.facebook.com/docs/threads/",
    },
    {
        "id": "bluesky",
        "name": "Bluesky",
        "content": "Text + up to 4 images",
        "limit": 300,
        "help": "Create an app password in Bluesky → Settings → Privacy and security → App passwords. No developer app review.",
        "docs": "https://bsky.app/settings/app-passwords",
    },
    {
        "id": "mastodon",
        "name": "Mastodon",
        "content": "Text + up to 4 images",
        "limit": 500,
        "help": "On your instance: Preferences → Development → New application. Grant read:accounts, write:statuses and write:media; copy your access token.",
        "docs": "https://docs.joinmastodon.org/client/token/",
    },
    {
        "id": "linkedin",
        "name": "LinkedIn",
        "content": "Personal-profile text + up to 4 images",
        "limit": 3000,
        "help": "Create a LinkedIn developer app and enable Share on LinkedIn and Sign In with LinkedIn using OpenID Connect. In the signed-in developer portal, open Docs and tools → Token Generator and choose member authorization and the openid, profile and w_member_social scopes. Paste the member access token here. Personal profiles only; company Pages are not supported.",
        "docs": "https://developer.linkedin.com/",
    },
    {
        "id": "x",
        "name": "X (Twitter)",
        "content": "Text + up to 4 images",
        "limit": 280,
        "help": "In the X (Twitter) Developer Console, enable OAuth 1.0a with Read and Write. Use Consumer Key, Consumer Secret, Access Token and Access Token Secret from OAuth 1.0 Keys, all from the same app. Generate the user tokens AFTER enabling write access. Do not use the OAuth 2.0 Client Secret or Bearer Token. API usage may cost money.",
        "docs": "https://docs.x.com/fundamentals/authentication/oauth-1-0a/api-key-and-secret",
    },
]
SUPPORTED = {p["id"] for p in PLATFORMS}
ROADMAP = [
    {
        "name": "TikTok",
        "reason": "Direct Post approval/audit; unaudited clients are restricted to private posts.",
    },
    {
        "name": "Pinterest / Google Business",
        "reason": "Separate developer access requirements; outside the first release.",
    },
]


def now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def bluesky_record_key(post):
    """Stable TID for retries, with per-post entropy below millisecond precision."""
    from datetime import datetime, timezone

    created = datetime.fromisoformat(post["createdAt"].replace("Z", "+00:00"))
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = created - epoch
    milliseconds = (elapsed.days * 86400 + elapsed.seconds) * 1000 + elapsed.microseconds // 1000
    # Spread posts sharing a millisecond across microseconds and 10-bit clock IDs.
    entropy = int.from_bytes(hashlib.sha256(post["id"].encode()).digest()[:4], "big")
    micros = milliseconds * 1000 + (entropy >> 10) % 1000
    value = (micros << 10) | (entropy & 1023)
    alphabet = "234567abcdefghijklmnopqrstuvwxyz"
    return "".join(alphabet[(value >> shift) & 31] for shift in range(60, -1, -5))


class ProviderError(Exception):
    def __init__(self, message, *, uncertain=False, status=0, code=0, subcode=0):
        super().__init__(message)
        self.uncertain = uncertain
        self.status = status
        self.code = code
        self.subcode = subcode


def platform_error(status, host, body=b""):
    """Retain numeric codes and allowlisted Discord validation fields, never raw error text."""
    code = subcode = 0
    if host == "discord.com":
        try:
            detail = json.loads(body)
            code = detail.get("code", 0)
            code = code if type(code) is int else 0
            fields = []
            allowed_fields = {
                "components",
                "items",
                "media",
                "url",
                "description",
                "content",
                "flags",
                "attachments",
                "allowed_mentions",
                "parse",
                "thread_id",
                "thread_name",
            }
            allowed_errors = {
                "BASE_TYPE_BAD_LENGTH",
                "BASE_TYPE_MAX_LENGTH",
                "BASE_TYPE_REQUIRED",
                "BASE_TYPE_CHOICES",
                "URL_TYPE_INVALID_URL",
                "STRING_TYPE_INVALID_URL",
                "INVALID_COMPONENT_TYPE",
            }

            def collect(node, path=(), depth=0):
                if not isinstance(node, dict) or depth > 12 or len(fields) >= 4:
                    return
                errors = node.get("_errors", [])
                if isinstance(errors, list):
                    for error in errors[:4]:
                        error_code = error.get("code") if isinstance(error, dict) else None
                        if error_code in allowed_errors:
                            fields.append((".".join(path) or "request") + " (" + error_code + ")")
                for key, value in node.items():
                    if key in allowed_fields or regex.fullmatch(r"[0-9]{1,2}", key):
                        collect(value, (*path, key), depth + 1)

            collect(detail.get("errors", {}))
            message = f"Discord rejected the request (HTTP {status})"
            if code:
                message += f". Discord error {code}"
            if fields:
                message += ": " + "; ".join(fields)
            return ProviderError(message + ".", status=status, code=code, uncertain=status >= 500)
        except (ValueError, AttributeError, TypeError, UnicodeError):
            pass
    if host in ("graph.instagram.com", "graph.threads.net", "graph.facebook.com"):
        try:
            detail = json.loads(body).get("error", {})
            code = detail.get("code", 0)
            subcode = detail.get("error_subcode", 0)
            code = code if type(code) is int else 0
            subcode = subcode if type(subcode) is int else 0
        except (ValueError, AttributeError, UnicodeError):
            pass
    message = (
        "Credentials expired or lack permission. Reconnect this account."
        if status in (401, 403)
        else "Platform rate limit reached. Try again later."
        if status == 429
        else f"Platform rejected the request (HTTP {status})."
    )
    if code:
        message += f" Meta error {code}" + (f", subcode {subcode}" if subcode else "") + "."
    return ProviderError(
        message, uncertain=status >= 500, status=status, code=code, subcode=subcode
    )


def public_ip(value):
    address = ipaddress.ip_address(value)
    return address.is_global and not address.is_multicast and not address.is_reserved


def instance_url(value):
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.port
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or "." not in url.hostname
            or url.hostname.endswith((".local", ".localhost"))
        ):
            raise ValueError()
        try:
            address = ipaddress.ip_address(url.hostname)
        except ValueError:
            pass
        else:
            if not public_ip(str(address)):
                raise ValueError()
        return f"https://{url.hostname}"
    except (ValueError, TypeError):
        raise ValueError("Use a public HTTPS server address without a path or port.") from None


def image_url(value):
    try:
        url = urlsplit(value)
        instance_url(f"{url.scheme}://{url.netloc}")
        if url.fragment or len(value) > 2048:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            "Use a direct public HTTPS image URL without a fragment or login credentials."
        ) from None


def discord_webhook(value):
    try:
        url = urlsplit(value)
        match = regex.fullmatch(
            r"/api/(?:v10/)?webhooks/(\d{5,25})/([A-Za-z0-9_.-]{20,200})/?", url.path
        )
        if (
            url.scheme != "https"
            or url.hostname
            not in ("discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com")
            or url.port
            or url.username
            or url.password
            or url.fragment
            or not match
        ):
            raise ValueError()
        query = dict(parse_qsl(url.query))
        if (
            set(query) - {"thread_id", "wait"}
            or query.get("thread_id")
            and not regex.fullmatch(r"\d{5,25}", query["thread_id"])
        ):
            raise ValueError()
        return {
            "webhook": f"https://discord.com/api/v10/webhooks/{match[1]}/{match[2]}",
            "webhookId": match[1],
            "threadId": query.get("thread_id", ""),
        }
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            "Paste an incoming Discord webhook URL copied from your server settings."
        ) from None


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        records = await super().resolve(host, port, family)
        if not records or any(not public_ip(r["host"]) for r in records):
            raise OSError("Platform address must resolve to public IPs.")
        return records


class Transport:
    def __init__(self):
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=6, ttl_dns_cache=60),
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=False,
            headers={"User-Agent": "LightPost/0.2 (self-hosted)"},
        )

    async def __call__(
        self,
        method,
        url,
        *,
        _binary=False,
        _receipt=False,
        _limit=2 * 1024 * 1024,
        _upload=False,
        **kwargs,
    ):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ProviderError("Invalid platform endpoint.")
        try:
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                pass
            else:
                if not public_ip(str(address)):
                    raise ProviderError("Private platform addresses are blocked.")
            async with self.session.request(
                method, url, allow_redirects=False, **kwargs
            ) as response:
                if not 200 <= response.status < 300 and not (_upload and response.status == 308):
                    error_body = bytearray()
                    if parsed.hostname in (
                        "graph.instagram.com",
                        "graph.threads.net",
                        "graph.facebook.com",
                        "discord.com",
                    ):
                        async for chunk in response.content.iter_chunked(8192):
                            error_body.extend(chunk)
                            if len(error_body) > 65536:
                                error_body.clear()
                                break
                    raise platform_error(response.status, parsed.hostname, error_body)
                content = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    content.extend(chunk)
                    if len(content) > _limit:
                        raise ProviderError(
                            "Platform response exceeded the size limit.", uncertain=True
                        )
                if _binary:
                    return bytes(content)
                try:
                    data = json.loads(content) if content else {}
                    if _receipt:
                        return {
                            "data": data,
                            "headers": {"x-restli-id": response.headers.get("x-restli-id", "")},
                        }
                    if _upload:
                        return {
                            "data": data,
                            "status": response.status,
                            "headers": {
                                name: response.headers.get(name, "")
                                for name in ("Location", "Range")
                            },
                        }
                    return data
                except (ValueError, UnicodeError):
                    raise ProviderError(
                        "Platform returned an unreadable receipt.", uncertain=True
                    ) from None
        except ProviderError:
            raise
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            raise ProviderError(
                "Connection interrupted. Check the platform before retrying.",
                uncertain=method != "GET",
            ) from None

    async def close(self):
        await self.session.close()


def oauth_header(method, url, credentials, *, nonce=None, timestamp=None):
    """RFC 5849 HMAC-SHA1; JSON and multipart bodies are not signed parameters."""

    def enc(value):
        return quote(str(value), safe="~-._")

    values = {
        "oauth_consumer_key": credentials["apiKey"],
        "oauth_token": credentials["token"],
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_timestamp": str(timestamp or int(time.time())),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_version": "1.0",
    }
    parts = urlsplit(url)
    pairs = sorted(
        (enc(k), enc(v))
        for k, v in [*values.items(), *parse_qsl(parts.query, keep_blank_values=True)]
    )
    normalized = "&".join(f"{k}={v}" for k, v in pairs)
    base_url = f"{parts.scheme}://{parts.netloc}{parts.path}"
    base = "&".join(enc(v) for v in (method.upper(), base_url, normalized))
    key = enc(credentials["apiSecret"]) + "&" + enc(credentials["tokenSecret"])
    values["oauth_signature"] = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()
    return "OAuth " + ", ".join(f'{enc(k)}="{enc(v)}"' for k, v in sorted(values.items()))


def x_headers(method, url, credentials):
    return {"Authorization": oauth_header(method, url, credentials)}


def text_length(platform, text):
    text = unicodedata.normalize("NFC", text)
    if platform == "bluesky":
        return len(regex.findall(r"\X", text))
    if platform == "telegram":
        return len(text.encode("utf-16-le")) // 2
    if platform != "x":
        return len(text)
    # Conservative for unusual URL syntax; the platform remains the final validator.
    total = 0
    for part in regex.split(r"(https?://[^\s]+)", text):
        if part.startswith(("https://", "http://")):
            total += 23
            continue
        for grapheme in regex.findall(r"\X", part):
            if regex.search(r"\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20e3", grapheme):
                total += 2
            else:
                total += sum(
                    1
                    if (
                        ord(c) <= 0x10FF
                        or 0x2000 <= ord(c) <= 0x200D
                        or 0x2010 <= ord(c) <= 0x201F
                        or 0x2032 <= ord(c) <= 0x2037
                    )
                    else 2
                    for c in grapheme
                )
    return total


def destination_post(account, post):
    if account["platform"] == "youtube":
        return {**post, "mediaIds": []}
    options = post.get("contentOptions", {}).get(account["id"], {})
    include_images = account["platform"] == "instagram" or options.get("image", True)
    return {
        **post,
        "text": post["text"] if account["platform"] == "x" or options.get("text", True) else "",
        "mediaIds": post.get("mediaIds", []) if include_images else [],
    }


def validate_post(account, post):
    if account.get("demo"):
        return
    if account["platform"] == "youtube":
        from .youtube import validate_video_post

        validate_video_post(post)
        return
    if not post["text"].strip() and not post.get("mediaIds"):
        raise ValueError(
            f"{account['name']} has no content. Enable Text or Image, or deselect the account."
        )
    if account["platform"] not in SUPPORTED:
        raise ValueError("Choose a supported platform.")
    if (
        account["platform"] == "x"
        and regex.search(
            r"https?://|www\.|(?:[\p{L}\p{N}-]+\.)+[\p{L}]{2,}(?:[/:?#]\S*)?", post["text"], regex.I
        )
        and post.get("xAllowLinks") is not True
    ):
        raise ValueError(
            "Links are disabled for X (Twitter). Enable Allow links or remove the URL."
        )
    if account["platform"] == "instagram" and len(post.get("mediaIds", [])) != 1:
        raise ValueError("Instagram needs one public JPEG image. Use Add image URL to attach it.")
    if account["platform"] == "threads" and len(post.get("mediaIds", [])) > 1:
        raise ValueError("Threads supports one image per post in this version.")
    limit = {
        "bluesky": 300,
        "mastodon": account.get("limit", 500),
        "x": 280,
        "instagram": 2200,
        "threads": 500,
        "discord": 2000,
        "linkedin": 3000,
        "telegram": 1024 if post.get("mediaIds") else 4096,
        "facebook": 10000,
    }[account["platform"]]
    if text_length(account["platform"], post["text"]) > limit:
        raise ValueError(
            f"{account['name']} allows {limit} characters (platform counting rules apply)."
        )


class Providers:
    def __init__(self, vault, send, pause=asyncio.sleep):
        self.vault, self.send, self.pause = vault, send, pause

    async def connect(self, data):
        platform = data.get("platform")
        if platform not in SUPPORTED:
            raise ValueError("Choose one of the supported platforms.")
        if platform == "youtube":
            from .youtube import connect_channel

            return await connect_channel(self, data)
        secret = str(data.get("secret", "")).strip()
        if not secret or len(secret) > 8192:
            raise ValueError("Enter your account's app password or access token.")
        if platform == "discord":
            credentials = discord_webhook(secret)
            profile_data = await self.send("GET", credentials["webhook"])
            if (
                profile_data.get("type") != 1
                or str(profile_data.get("id")) != credentials["webhookId"]
                or not profile_data.get("guild_id")
            ):
                raise ValueError("Use an incoming webhook for a Discord server channel.")
            credentials.update(
                guildId=str(profile_data["guild_id"]), channelId=str(profile_data["channel_id"])
            )
            profile = {
                "remoteId": credentials["webhookId"]
                + (":" + credentials["threadId"] if credentials["threadId"] else ""),
                "handle": "#" + str(profile_data.get("name") or "webhook"),
                "name": "Discord",
            }
        elif platform == "facebook":
            page_id = str(data.get("pageId", "")).strip()
            if not regex.fullmatch(r"[0-9]{5,25}", page_id):
                raise ValueError("Enter the numeric Facebook Page ID.")
            credentials = {"token": secret, "pageId": page_id}
            try:
                page = await self.facebook_call(
                    credentials, "GET", "me", params={"fields": "id,name,category"}
                )
            except ProviderError as error:
                if error.status == 400 and error.code not in (10, 190, 200):
                    raise ProviderError(
                        "Facebook could not verify this Page. Use its Page access token, not a user or Instagram token, and check the Page permissions.",
                        status=error.status,
                        code=error.code,
                        subcode=error.subcode,
                    ) from None
                raise
            if (
                not isinstance(page, dict)
                or str(page.get("id")) != page_id
                or not page.get("category")
                or not page.get("name")
            ):
                raise ValueError(
                    "The token must belong to the Facebook Page ID entered here. Generate a Page access token for that Page."
                )
            profile = {
                "remoteId": page_id,
                "handle": str(page["name"])[:150],
                "name": "Facebook Pages",
            }
        elif platform == "telegram":
            channel = str(data.get("chatId", "")).strip()
            if not regex.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}|-100[0-9]{5,20}", channel):
                raise ValueError(
                    "Enter the channel’s @username or numeric ID beginning -100. Invite links are not channel IDs."
                )
            credentials = {"token": secret}
            bot = await self.telegram_call(credentials, "getMe")
            if (
                not isinstance(bot, dict)
                or bot.get("is_bot") is not True
                or type(bot.get("id")) is not int
            ):
                raise ValueError("Telegram did not return a valid bot identity.")
            chat = await self.telegram_call(credentials, "getChat", params={"chat_id": channel})
            if (
                not isinstance(chat, dict)
                or chat.get("type") != "channel"
                or not regex.fullmatch(r"-100[0-9]{5,20}", str(chat.get("id", "")))
            ):
                raise ValueError(
                    "Choose a Telegram channel. Groups and private bot conversations are not supported."
                )
            member = await self.telegram_call(
                credentials,
                "getChatMember",
                params={"chat_id": str(chat["id"]), "user_id": str(bot["id"])},
            )
            if (
                not isinstance(member, dict)
                or member.get("user", {}).get("id") != bot["id"]
                or not (
                    member.get("status") == "creator"
                    or member.get("status") == "administrator"
                    and member.get("can_post_messages") is True
                )
            ):
                raise ValueError(
                    "Add this bot as a channel administrator and enable Post Messages before connecting."
                )
            username = str(chat.get("username", ""))
            username = username if regex.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username) else ""
            credentials.update(chatId=str(chat["id"]), botId=bot["id"], channelUsername=username)
            profile = {
                "remoteId": str(chat["id"]),
                "name": "Telegram",
                "handle": "@" + username
                if username
                else str(chat.get("title") or "Private channel")[:150],
            }
        elif platform in ("instagram", "threads"):
            credentials = {"token": secret, "lastRefreshAt": time.time()}
            fields = "user_id,username" if platform == "instagram" else "id,username"
            profile_data = await self.send(
                "GET",
                self.meta_base(platform) + "/me",
                headers={"Authorization": "Bearer " + secret},
                params={"fields": fields},
            )
            remote_id = profile_data.get("user_id") or profile_data.get("id")
            if not remote_id or not profile_data.get("username"):
                raise ValueError(
                    "Cannot verify this account. Check the platform, token and app permissions."
                )
            credentials["userId"] = str(remote_id)
            profile = {
                "remoteId": str(remote_id),
                "handle": "@" + profile_data["username"],
                "name": "Instagram" if platform == "instagram" else "Threads",
            }
        elif platform == "linkedin":
            credentials = {"token": secret}
            try:
                identity = await self.send(
                    "GET",
                    "https://api.linkedin.com/v2/userinfo",
                    headers={"Authorization": "Bearer " + secret},
                )
            except ProviderError as error:
                if error.status in (401, 403):
                    raise ProviderError(
                        "LinkedIn could not verify the token. Enable the OpenID Connect and Share on LinkedIn products, then generate a member token with openid, profile and w_member_social.",
                        status=error.status,
                    ) from None
                raise
            subject = str(identity.get("sub", ""))
            if not regex.fullmatch(r"[A-Za-z0-9_-]{1,128}", subject):
                raise ValueError("LinkedIn did not return a valid member identity.")
            profile = {
                "remoteId": subject,
                "handle": str(identity.get("name") or "LinkedIn member"),
                "name": "LinkedIn",
            }
        elif platform == "bluesky":
            instance = instance_url(data.get("instance") or "https://bsky.social")
            credentials = {
                "identifier": str(data.get("handle", "")).strip().lstrip("@"),
                "password": secret,
                "instance": instance,
            }
            session = await self.send(
                "POST",
                instance + "/xrpc/com.atproto.server.createSession",
                json={"identifier": credentials["identifier"], "password": secret},
            )
            credentials.update(accessJwt=session["accessJwt"], refreshJwt=session["refreshJwt"])
            profile = {
                "remoteId": session["did"],
                "handle": "@" + session["handle"],
                "name": "Bluesky",
            }
        elif platform == "mastodon":
            instance = instance_url(data.get("instance", ""))
            credentials = {"instance": instance, "token": secret}
            profile_data = await self.send(
                "GET",
                instance + "/api/v1/accounts/verify_credentials",
                headers={"Authorization": "Bearer " + secret},
            )
            profile = {
                "remoteId": str(profile_data["id"]),
                "handle": "@" + profile_data["acct"],
                "name": "Mastodon",
                "limit": 500,
            }
        else:
            credentials = {"token": secret}
            for field in ("apiKey", "apiSecret", "tokenSecret"):
                value = str(data.get(field, "")).strip()
                if not value or len(value) > 8192:
                    raise ValueError("Enter all four X (Twitter) developer credentials.")
                credentials[field] = value
            url = "https://api.x.com/2/users/me"
            try:
                result = await self.send("GET", url, headers=x_headers("GET", url, credentials))
            except ProviderError as error:
                if error.status in (401, 403):
                    raise ProviderError(
                        f"X (Twitter) rejected account verification (HTTP {error.status}). "
                        "Use all four credentials from OAuth 1.0 Keys in the same app: "
                        "Consumer Key, Consumer Secret, Access Token and Access Token Secret. "
                        "The OAuth 2.0 Client Secret is different. Check Read and Write access "
                        "and regenerate the user token pair if permissions changed. "
                        "Also check X (Twitter) API access restrictions and your server clock.",
                        status=error.status,
                    ) from None
                raise
            profile = {
                "remoteId": str(result["data"]["id"]),
                "handle": "@" + result["data"]["username"],
                "name": "X (Twitter)",
            }
        return {
            **profile,
            "platform": platform,
            "demo": False,
            "credentials": self.vault.encrypt(credentials),
        }

    @staticmethod
    def meta_base(platform):
        if platform == "threads":
            return "https://graph.threads.net/v1.0"
        version = os.getenv("META_GRAPH_VERSION", "v25.0")
        if not regex.fullmatch(r"v\d+\.\d+", version):
            raise ValueError("META_GRAPH_VERSION must look like v25.0.")
        return "https://graph.instagram.com/" + version

    async def publish_meta(
        self, account, post, images, credentials, save_credentials, previous, checkpoint
    ):
        platform = account["platform"]
        base = self.meta_base(platform)
        if images and (
            len(images) != 1 or images[0]["mime"] != "image/jpeg" or not images[0].get("sourceUrl")
        ):
            raise ValueError(
                "Instagram and Threads images need one public JPEG URL. Use Add image URL."
            )
        if time.time() - credentials.get("lastRefreshAt", time.time()) > 7 * 86400:
            host = (
                "https://graph.instagram.com"
                if platform == "instagram"
                else "https://graph.threads.net"
            )
            refreshed = await self.send(
                "GET",
                host + "/refresh_access_token",
                params={
                    "grant_type": "ig_refresh_token"
                    if platform == "instagram"
                    else "th_refresh_token",
                    "access_token": credentials["token"],
                },
            )
            if not refreshed.get("access_token"):
                raise ProviderError("Token refresh failed. Reconnect this account.")
            credentials.update(token=refreshed["access_token"], lastRefreshAt=time.time())
            await save_credentials(credentials)
        headers = {"Authorization": "Bearer " + credentials["token"]}
        user_id = str(credentials.get("userId") or account.get("remoteId", ""))
        if not regex.fullmatch(r"\d+", user_id):
            raise ValueError("Reconnect this account to verify its publishing identity.")
        container = (
            (previous or {}).get("containerId")
            if (previous or {}).get("stage") != "rejected"
            else None
        )
        created = (previous or {}).get("containerCreatedAt", time.time())
        if not container:
            if platform == "instagram":
                payload = {
                    "image_url": image_url(images[0]["sourceUrl"]),
                    "caption": post["text"],
                    "alt_text": images[0].get("alt", ""),
                }
                path = f"/{user_id}/media"
            else:
                payload = {"media_type": "IMAGE" if images else "TEXT", "text": post["text"]}
                if images:
                    payload.update(
                        image_url=image_url(images[0]["sourceUrl"]),
                        alt_text=images[0].get("alt", ""),
                    )
                path = f"/{user_id}/threads"
            try:
                result = await self.send("POST", base + path, headers=headers, data=payload)
            except ProviderError as error:
                # Creating a container does not publish a post; this failure is safe to retry.
                raise ProviderError(
                    "Could not prepare the post. Check token permissions and the public image URL.",
                    status=error.status,
                ) from None
            container = result.get("id")
            if not container or not regex.fullmatch(r"\d+", str(container)):
                raise ProviderError("The platform did not return a valid publishing container.")
            created = time.time()
        retry_request = post.get("retryRequestedAt", 0)
        handled_retry = (previous or {}).get("retryRequestHandledAt", 0)
        explicit_retry = retry_request > handled_retry
        pending = {
            "retryRequestHandledAt": max(retry_request, handled_retry),
            "status": "processing",
            "stage": "container",
            "containerId": str(container),
            "containerCreatedAt": created,
            "message": "Prepared on the platform; waiting for media processing.",
        }

        async def verify_publication(error=None, allow_retry=False):
            started = (previous or {}).get("verificationStartedAt") or time.time()
            verifying = {
                **pending,
                "stage": "verifying",
                "verificationStartedAt": started,
                "message": "Checking the platform's publication response.",
            }
            for key in ("httpStatus", "providerCode", "providerSubcode"):
                if key in (previous or {}):
                    verifying[key] = previous[key]
            if error:
                verifying.update(
                    httpStatus=error.status, providerCode=error.code, providerSubcode=error.subcode
                )
            if checkpoint:
                await checkpoint(verifying)
            if error or not (previous or {}).get("verificationStartedAt"):
                await self.pause(2)
            try:
                confirmation = await self.send(
                    "GET",
                    base + "/" + str(container),
                    headers=headers,
                    params={"fields": "status_code" if platform == "instagram" else "status"},
                )
                confirmed = confirmation.get("status_code" if platform == "instagram" else "status")
            except ProviderError:
                confirmed = None
            if confirmed == "PUBLISHED":
                return {
                    "status": "published",
                    "containerId": str(container),
                    "message": "Platform confirmed this container was published.",
                }
            if confirmed in ("ERROR", "EXPIRED"):
                return {
                    **verifying,
                    "status": "failed",
                    "stage": "rejected",
                    "message": "Platform confirmed that publishing failed or the unpublished container expired.",
                }
            if allow_retry and confirmed in ("FINISHED", "IN_PROGRESS"):
                return None
            if (
                error
                and platform == "instagram"
                and error.status == 400
                and (error.code, error.subcode) == (9007, 2207027)
                and confirmed in ("FINISHED", "IN_PROGRESS")
            ):
                return {
                    **pending,
                    "status": "failed",
                    "message": "Instagram says the image is not ready to publish. Try again shortly.",
                }
            return {
                **verifying,
                "status": "uncertain",
                "message": "Publication was not confirmed. Check Instagram before retrying; retrying may create another post."
                if platform == "instagram"
                else "Publication was not confirmed. Check Threads before retrying; retrying may create another post.",
            }

        if (previous or {}).get("stage") in ("publishing", "verifying"):
            verification = await verify_publication(allow_retry=explicit_retry)
            if verification is not None:
                return verification
        if checkpoint:
            await checkpoint(pending)
        if time.time() - created > 24 * 3600:
            raise ProviderError("The unpublished container expired. Retry to prepare a new one.")
        try:
            result = await self.send(
                "GET",
                base + "/" + str(container),
                headers=headers,
                params={"fields": "status_code" if platform == "instagram" else "status"},
            )
        except ProviderError as error:
            # Read-only polling can resume safely after network errors; auth failures need attention.
            if error.status in (400, 401, 403, 404):
                raise
            return {**pending, "message": "Status check unavailable; will check again shortly."}
        status = result.get("status_code" if platform == "instagram" else "status")
        if status == "PUBLISHED":
            return {
                "status": "published",
                "containerId": str(container),
                "message": "Platform confirmed this container was published.",
            }
        if status in ("ERROR", "EXPIRED"):
            raise ProviderError(
                "The platform could not prepare the media or its container expired. Check the image URL and retry."
            )
        if status != "FINISHED":
            return pending
        if platform == "instagram":
            await self.pause(2)
        if checkpoint:
            await checkpoint(
                {
                    **pending,
                    "status": "sending",
                    "stage": "publishing",
                    "message": "Submitting the prepared post for publication.",
                }
            )
        path = (
            f"/{user_id}/media_publish"
            if platform == "instagram"
            else f"/{user_id}/threads_publish"
        )
        try:
            result = await self.send(
                "POST", base + path, headers=headers, data={"creation_id": str(container)}
            )
        except ProviderError as error:
            return await verify_publication(error)
        if not result.get("id"):
            return await verify_publication()
        receipt = {
            "status": "published",
            "remoteId": str(result["id"]),
            "containerId": str(container),
        }
        try:
            link = await self.send(
                "GET",
                base + "/" + str(result["id"]),
                headers=headers,
                params={"fields": "permalink"},
            )
            if link.get("permalink"):
                receipt["url"] = link["permalink"]
        except ProviderError:
            pass  # A missing permalink never turns a confirmed publication into a failure.
        return receipt

    @staticmethod
    def facebook_base():
        version = os.getenv("META_GRAPH_VERSION", "v25.0")
        if not regex.fullmatch(r"v[0-9]+\.[0-9]+", version):
            raise ValueError("META_GRAPH_VERSION must look like v25.0.")
        return "https://graph.facebook.com/" + version

    async def facebook_call(self, credentials, method, path, **kwargs):
        try:
            return await self.send(
                method,
                self.facebook_base() + "/" + path,
                headers={"Authorization": "Bearer " + credentials["token"]},
                **kwargs,
            )
        except ProviderError as error:
            if error.code == 190 or error.status == 401:
                message = "Facebook rejected the Page access token. It may be expired or revoked; reconnect this Page."
            elif error.code in (10, 200) or error.status == 403:
                message = "Facebook denied Page access. Check pages_manage_posts, pages_read_engagement and pages_show_list, and your permission to create content on this Page."
            else:
                message = (
                    "Facebook rejected the request"
                    + (f" (HTTP {error.status})" if error.status else "")
                    + "."
                )
                if error.code:
                    message += (
                        f" Meta error {error.code}"
                        + (f", subcode {error.subcode}" if error.subcode else "")
                        + "."
                    )
            raise ProviderError(
                message,
                status=error.status,
                code=error.code,
                subcode=error.subcode,
                uncertain=error.uncertain,
            ) from None

    async def publish_facebook(self, account, post, images, credentials, previous, checkpoint):
        page_id = str(credentials.get("pageId", ""))
        if not regex.fullmatch(r"[0-9]{5,25}", page_id) or page_id != str(account.get("remoteId")):
            raise ValueError("Reconnect Facebook to verify the Page ID and token.")
        if len(images) > 4 or any(
            image["mime"] not in ("image/jpeg", "image/png") for image in images
        ):
            raise ValueError("Facebook Pages supports up to four JPEG or PNG images in this app.")

        def receipt(post_id):
            suffix = post_id.split("_")[-1]
            return {
                "status": "published",
                "remoteId": post_id,
                "url": f"https://www.facebook.com/{page_id}/posts/{suffix}",
            }

        # A known post must never be created a second time just to check its state.
        known = str((previous or {}).get("remoteId", ""))
        if regex.fullmatch(page_id + r"_[0-9]+", known):
            try:
                confirmation = await self.facebook_call(
                    credentials, "GET", known, params={"fields": "is_published"}
                )
            except ProviderError:
                confirmation = {}
            if confirmation.get("is_published") is True:
                return receipt(known)
            return {
                **receipt(known),
                "status": "uncertain",
                "message": "Facebook has a post receipt but has not confirmed publication. Check the Page before retrying.",
            }

        media_ids = post.get("mediaIds", [])
        photo_ids = (
            (previous or {}).get("photoIds", [])
            if (previous or {}).get("photoMediaIds") == media_ids
            else []
        )
        if (
            not isinstance(photo_ids, list)
            or len(photo_ids) > len(images)
            or any(not regex.fullmatch(r"[0-9]+", str(id)) for id in photo_ids)
        ):
            photo_ids = []
        photo_ids = list(photo_ids)
        for index in range(len(photo_ids), len(images)):
            if checkpoint:
                await checkpoint(
                    {
                        "stage": "container",
                        "status": "sending",
                        "photoIds": list(photo_ids),
                        "photoMediaIds": media_ids,
                        "message": "Preparing images on Facebook.",
                    }
                )
            image = images[index]
            form = aiohttp.FormData()
            form.add_field("published", "false")
            form.add_field(
                "source",
                image["bytes"],
                filename=f"image-{index}." + ("jpg" if image["mime"] == "image/jpeg" else "png"),
                content_type=image["mime"],
            )
            try:
                uploaded = await self.facebook_call(
                    credentials, "POST", page_id + "/photos", data=form
                )
            except ProviderError as error:
                # This upload is explicitly unpublished, so it cannot have created a feed post.
                raise ProviderError(
                    "Could not prepare the Facebook image. " + str(error),
                    status=error.status,
                    code=error.code,
                    subcode=error.subcode,
                ) from None
            photo_id = str(uploaded.get("id", ""))
            if not regex.fullmatch(r"[0-9]+", photo_id):
                raise ProviderError(
                    "Facebook did not confirm the image upload. The feed post was not sent."
                )
            photo_ids.append(photo_id)
            if checkpoint:
                await checkpoint(
                    {
                        "stage": "container",
                        "status": "sending",
                        "photoIds": list(photo_ids),
                        "photoMediaIds": media_ids,
                        "message": "Image prepared on Facebook.",
                    }
                )
        if checkpoint:
            await checkpoint(
                {
                    "stage": "publishing",
                    "status": "sending",
                    "photoIds": photo_ids,
                    "photoMediaIds": media_ids,
                    "message": "Publishing to the Facebook Page.",
                }
            )
        payload = {"published": "true"}
        if post["text"]:
            payload["message"] = post["text"]
        if photo_ids:
            payload["attached_media"] = json.dumps([{"media_fbid": id} for id in photo_ids])
        try:
            result = await self.facebook_call(credentials, "POST", page_id + "/feed", data=payload)
        except ProviderError as error:
            uncertain = (
                error.uncertain
                or error.status not in (401, 403, 429)
                and error.code not in (10, 100, 190, 200)
            )
            raise ProviderError(
                str(error)
                + (
                    " Check the Page before retrying; publication was not confirmed."
                    if uncertain
                    else ""
                ),
                status=error.status,
                code=error.code,
                subcode=error.subcode,
                uncertain=uncertain,
            ) from None
        post_id = str(result.get("id", ""))
        if not regex.fullmatch(page_id + r"_[0-9]+", post_id):
            raise ProviderError(
                "Facebook returned no reliable post receipt. Check the Page before retrying.",
                uncertain=True,
            )
        published = receipt(post_id)
        try:
            confirmation = await self.facebook_call(
                credentials, "GET", post_id, params={"fields": "is_published"}
            )
            if confirmation.get("is_published") is False:
                return {
                    **published,
                    "status": "uncertain",
                    "message": "Facebook created the post but has not marked it published. Check the Page before retrying.",
                }
        except ProviderError:
            pass  # A failed read does not undo the successful published=true creation receipt.
        return published

    async def telegram_call(self, credentials, operation, *, method="GET", **kwargs):
        token = str(credentials.get("token", ""))
        if not regex.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,128}", token):
            raise ValueError("Enter a valid Telegram bot token from @BotFather.")
        if operation not in {
            "getMe",
            "getChat",
            "getChatMember",
            "sendMessage",
            "sendPhoto",
            "sendMediaGroup",
        }:
            raise ValueError("Unsupported Telegram operation.")
        try:
            response = await self.send(
                method, "https://api.telegram.org/bot" + token + "/" + operation, **kwargs
            )
        except ProviderError as error:
            code, uncertain = error.status, error.uncertain
        else:
            if isinstance(response, dict) and response.get("ok") is True and "result" in response:
                return response["result"]
            code = response.get("error_code", 0) if isinstance(response, dict) else 0
            code = code if type(code) is int else 0
            uncertain = code >= 500 or not code and method != "GET"
        # Never echo Telegram descriptions or URLs: the endpoint contains the bot token.
        message = (
            "Telegram rejected the bot token. Generate a new token with @BotFather and reconnect."
            if code == 401
            else "Telegram rate limit reached. Try again later."
            if code == 429
            else "Telegram denied access. Add the bot as a channel administrator with Post Messages enabled."
            if code == 403
            else "Telegram could not find the channel. Check its @username or numeric ID and add the bot as an administrator."
            if operation in ("getChat", "getChatMember") and code == 400
            else "Telegram rejected the message. Check the channel permissions, text length and image format."
            if code == 400
            else "Telegram did not confirm the request. Check the channel before retrying."
        )
        raise ProviderError(message, status=code, uncertain=uncertain) from None

    async def publish_telegram(self, account, post, images, credentials, checkpoint):
        channel = str(credentials.get("chatId", ""))
        if not regex.fullmatch(r"-100[0-9]{5,20}", channel) or channel != str(
            account.get("remoteId")
        ):
            raise ValueError("Reconnect Telegram to verify its channel.")
        if len(images) > 4 or any(
            image["mime"] not in ("image/jpeg", "image/png") for image in images
        ):
            raise ValueError("Telegram supports up to four JPEG or PNG images in this app.")
        if checkpoint:
            await checkpoint(
                {
                    "stage": "publishing",
                    "status": "sending",
                    "message": "Posting to the Telegram channel.",
                }
            )
        if not images:
            operation = "sendMessage"
            kwargs = {"json": {"chat_id": channel, "text": post["text"]}}
        else:
            form = aiohttp.FormData()
            form.add_field("chat_id", channel)
            if len(images) == 1:
                operation = "sendPhoto"
                if post["text"]:
                    form.add_field("caption", post["text"])
                image = images[0]
                form.add_field(
                    "photo",
                    image["bytes"],
                    filename="image." + ("jpg" if image["mime"] == "image/jpeg" else "png"),
                    content_type=image["mime"],
                )
            else:
                operation = "sendMediaGroup"
                media = []
                for index, image in enumerate(images):
                    item = {"type": "photo", "media": f"attach://image{index}"}
                    if index == 0 and post["text"]:
                        item["caption"] = post["text"]
                    media.append(item)
                    form.add_field(
                        f"image{index}",
                        image["bytes"],
                        filename=f"image-{index}."
                        + ("jpg" if image["mime"] == "image/jpeg" else "png"),
                        content_type=image["mime"],
                    )
                form.add_field("media", json.dumps(media))
            kwargs = {"data": form}
        result = await self.telegram_call(credentials, operation, method="POST", **kwargs)
        messages = result if isinstance(result, list) else [result]
        expected = len(images) if len(images) > 1 else 1
        if len(messages) != expected or any(
            not isinstance(m, dict)
            or type(m.get("message_id")) is not int
            or m["message_id"] <= 0
            or str(m.get("chat", {}).get("id")) != channel
            for m in messages
        ):
            raise ProviderError(
                "Telegram returned no reliable publication receipt. Check the channel before retrying.",
                uncertain=True,
            )
        first = messages[0]
        username = str(first.get("chat", {}).get("username", ""))
        path = (
            username
            if regex.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username)
            else "c/" + channel[4:]
        )
        return {
            "status": "published",
            "remoteId": str(first["message_id"]),
            "messageIds": [m["message_id"] for m in messages],
            "url": f"https://t.me/{path}/{first['message_id']}",
        }

    async def publish_linkedin(self, account, post, images, credentials, checkpoint):
        member = str(account.get("remoteId", ""))
        if not regex.fullmatch(r"[A-Za-z0-9_-]{1,128}", member):
            raise ValueError("Reconnect LinkedIn to verify the publishing identity.")
        author = "urn:li:person:" + member
        headers = {
            "Authorization": "Bearer " + credentials["token"],
            "X-Restli-Protocol-Version": "2.0.0",
        }
        media = []
        for image in images:
            if checkpoint:
                await checkpoint(
                    {
                        "stage": "container",
                        "status": "sending",
                        "message": "Uploading image to LinkedIn.",
                    }
                )
            registered = await self.send(
                "POST",
                "https://api.linkedin.com/v2/assets?action=registerUpload",
                headers=headers,
                json={
                    "registerUploadRequest": {
                        "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                        "owner": author,
                        "serviceRelationships": [
                            {
                                "relationshipType": "OWNER",
                                "identifier": "urn:li:userGeneratedContent",
                            }
                        ],
                    }
                },
            )
            value = registered.get("value", {})
            upload = (
                value.get("uploadMechanism", {})
                .get("com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {})
                .get("uploadUrl", "")
            )
            parsed = urlsplit(upload)
            asset = value.get("asset", "")
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or not parsed.hostname
                or not (
                    parsed.hostname == "linkedin.com" or parsed.hostname.endswith(".linkedin.com")
                )
                or not regex.fullmatch(r"urn:li:digitalmediaAsset:[A-Za-z0-9_-]+", str(asset))
            ):
                raise ProviderError("LinkedIn returned an invalid image upload destination.")
            await self.send(
                "PUT",
                upload,
                headers={"Authorization": headers["Authorization"], "Content-Type": image["mime"]},
                data=image["bytes"],
                _binary=True,
            )
            media.append(
                {"status": "READY", "media": asset, "description": {"text": image.get("alt", "")}}
            )
        content = {
            "shareCommentary": {"text": post["text"]},
            "shareMediaCategory": "IMAGE" if media else "NONE",
        }
        if media:
            content["media"] = media
        if checkpoint:
            await checkpoint(
                {"stage": "publishing", "status": "sending", "message": "Publishing to LinkedIn."}
            )
        response = await self.send(
            "POST",
            "https://api.linkedin.com/v2/ugcPosts",
            headers=headers,
            json={
                "author": author,
                "lifecycleState": "PUBLISHED",
                "specificContent": {"com.linkedin.ugc.ShareContent": content},
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            },
            _receipt=True,
        )
        urn = response.get("headers", {}).get("x-restli-id") or response.get("data", {}).get(
            "id", ""
        )
        if not regex.fullmatch(r"urn:li:(ugcPost|share):[0-9]+", str(urn)):
            raise ProviderError(
                "LinkedIn returned no publication receipt. Check LinkedIn before retrying.",
                uncertain=True,
            )
        return {
            "status": "published",
            "remoteId": urn,
            "url": "https://www.linkedin.com/feed/update/" + quote(urn, safe=":") + "/",
        }

    async def publish(
        self, account, post, images, save_credentials, previous=None, checkpoint=None
    ):
        if account.get("demo"):
            return {"status": "simulated", "message": "Demo only. No external post was created."}
        validate_post(account, post)
        credentials = self.vault.decrypt(account["credentials"])
        platform = account["platform"]
        if platform == "youtube":
            from .youtube import publish_video

            return await publish_video(
                self, account, post, credentials, save_credentials, previous, checkpoint
            )
        if platform == "discord":
            endpoint = discord_webhook(credentials["webhook"])["webhook"]
            if any(not image.get("sourceUrl") for image in images):
                raise ValueError(
                    "Discord images need a public image URL. Use Add Image to import one."
                )
            components = []
            if post["text"]:
                components.append({"type": 10, "content": post["text"]})
            if images:
                components.append(
                    {
                        "type": 12,
                        "items": [
                            {
                                "media": {"url": image_url(image["sourceUrl"])},
                                **({"description": image["alt"]} if image.get("alt") else {}),
                            }
                            for image in images
                        ],
                    }
                )
            payload = {
                "flags": 1 << 15,
                "components": [{"type": 17, "components": components}],
                "allowed_mentions": {"parse": []},
            }
            params = {"wait": "true", "with_components": "true"}
            if credentials.get("threadId"):
                params["thread_id"] = credentials["threadId"]
            result = await self.send("POST", endpoint, params=params, json=payload)
            if not result.get("id"):
                raise ProviderError(
                    "Discord did not return a message receipt. Check the channel before retrying.",
                    uncertain=True,
                )
            return {
                "status": "published",
                "url": f"https://discord.com/channels/{credentials['guildId']}/{result.get('channel_id', credentials['channelId'])}/{result['id']}",
            }
        if platform in ("instagram", "threads"):
            return await self.publish_meta(
                account, post, images, credentials, save_credentials, previous, checkpoint
            )
        if platform == "facebook":
            return await self.publish_facebook(
                account, post, images, credentials, previous, checkpoint
            )
        if platform == "telegram":
            return await self.publish_telegram(account, post, images, credentials, checkpoint)
        if platform == "linkedin":
            return await self.publish_linkedin(account, post, images, credentials, checkpoint)
        if platform == "bluesky":
            instance = credentials.get("instance", "https://bsky.social")
            session = None
            if credentials.get("refreshJwt"):
                try:
                    session = await self.send(
                        "POST",
                        instance + "/xrpc/com.atproto.server.refreshSession",
                        headers={"Authorization": "Bearer " + credentials["refreshJwt"]},
                    )
                except ProviderError as error:
                    if error.status not in (400, 401):
                        raise
            if session is None:
                session = await self.send(
                    "POST",
                    instance + "/xrpc/com.atproto.server.createSession",
                    json={
                        "identifier": credentials["identifier"],
                        "password": credentials["password"],
                    },
                )
            credentials.update(accessJwt=session["accessJwt"], refreshJwt=session["refreshJwt"])
            await save_credentials(credentials)
            headers = {"Authorization": "Bearer " + session["accessJwt"]}
            record = {
                "$type": "app.bsky.feed.post",
                "text": post["text"],
                "createdAt": post["deliveryAt"],
            }
            # URL facets use UTF-8 byte offsets, not Python character indices.
            facets = []
            for match in regex.finditer(r"https?://[^\s]+", post["text"]):
                link = match.group().rstrip(".,!;)")
                start = len(post["text"][: match.start()].encode())
                facets.append(
                    {
                        "index": {"byteStart": start, "byteEnd": start + len(link.encode())},
                        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": link}],
                    }
                )
            if facets:
                record["facets"] = facets
            embeds = []
            for image in images:
                blob = await self.send(
                    "POST",
                    instance + "/xrpc/com.atproto.repo.uploadBlob",
                    headers={**headers, "Content-Type": image["mime"]},
                    data=image["bytes"],
                )
                embeds.append({"alt": image.get("alt", ""), "image": blob["blob"]})
            if embeds:
                record["embed"] = {"$type": "app.bsky.embed.images", "images": embeds}
            rkey = bluesky_record_key(post)
            result = await self.send(
                "POST",
                instance + "/xrpc/com.atproto.repo.putRecord",
                headers=headers,
                json={
                    "repo": session["did"],
                    "collection": "app.bsky.feed.post",
                    "rkey": rkey,
                    "record": record,
                },
            )
            return {
                "status": "published",
                "uri": result["uri"],
                "url": f"https://bsky.app/profile/{session['did']}/post/{rkey}",
            }
        if platform == "mastodon":
            headers = {"Authorization": "Bearer " + credentials["token"]}
            media_ids = []
            for image in images:
                form = aiohttp.FormData()
                form.add_field(
                    "file", image["bytes"], filename=image["name"], content_type=image["mime"]
                )
                form.add_field("description", image.get("alt", ""))
                attachment = await self.send(
                    "POST", credentials["instance"] + "/api/v1/media", headers=headers, data=form
                )
                media_ids.append(attachment["id"])
            result = await self.send(
                "POST",
                credentials["instance"] + "/api/v1/statuses",
                headers={**headers, "Idempotency-Key": post["id"] + ":" + account["id"]},
                json={"status": post["text"], "visibility": "public", "media_ids": media_ids},
            )
            return {"status": "published", "url": result["url"]}
        return await self.publish_x(account, post, images, credentials, checkpoint)

    async def publish_x(self, account, post, images, credentials, checkpoint):
        if len(images) > 4 or any(
            image.get("mime") not in ("image/jpeg", "image/png") for image in images
        ):
            raise ValueError("X (Twitter) supports up to four JPEG or PNG images in this app.")
        media_ids = []
        upload_url = "https://api.x.com/2/media/upload"
        for image in images:
            if checkpoint:
                await checkpoint(
                    {
                        "status": "sending",
                        "stage": "container",
                        "message": "Uploading an image to X (Twitter).",
                    }
                )
            try:
                uploaded = await self.send(
                    "POST",
                    upload_url,
                    headers=x_headers("POST", upload_url, credentials),
                    json={
                        "media": base64.b64encode(image["bytes"]).decode(),
                        "media_category": "tweet_image",
                    },
                )
                data = uploaded.get("data", {})
                media_id = str(data.get("id", ""))
                if not regex.fullmatch(r"[0-9]+", media_id) or uploaded.get("errors"):
                    raise ProviderError("X (Twitter) did not confirm the image upload.")
                info = data.get("processing_info") or {}
                for attempt in range(10):
                    state = info.get("state")
                    if not state or state == "succeeded":
                        break
                    if state not in ("pending", "in_progress"):
                        raise ProviderError("X (Twitter) could not process this image.")
                    if checkpoint:
                        await checkpoint(
                            {
                                "status": "sending",
                                "stage": "container",
                                "message": "X (Twitter) is processing an image.",
                            }
                        )
                    delay = info.get("check_after_secs", 3)
                    await self.pause(min(10, max(1, delay if type(delay) in (int, float) else 3)))
                    status_url = (
                        upload_url + "?" + urlencode({"command": "STATUS", "media_id": media_id})
                    )
                    status = await self.send(
                        "GET", status_url, headers=x_headers("GET", status_url, credentials)
                    )
                    info = status.get("data", {}).get("processing_info") or {}
                if info.get("state") not in (None, "succeeded"):
                    raise ProviderError(
                        "X (Twitter) has not finished processing the image. Try again shortly."
                    )
                media_ids.append(media_id)
            except ProviderError as error:
                # Uploading alone cannot create a tweet. Never silently drop a failed image.
                message = (
                    "X (Twitter) denied the image upload. Check media-upload access and your app's Read and Write permissions, then reconnect if needed."
                    if error.status in (401, 403)
                    else "Could not upload the image to X (Twitter). The post was not sent. Try again shortly."
                )
                raise ProviderError(message, status=error.status) from None
        if checkpoint:
            await checkpoint(
                {
                    "status": "sending",
                    "stage": "publishing",
                    "message": "Publishing to X (Twitter).",
                }
            )
        payload = {"text": post["text"]} if post["text"] else {}
        if media_ids:
            payload["media"] = {"media_ids": media_ids}
        url = "https://api.x.com/2/tweets"
        try:
            result = await self.send(
                "POST",
                url,
                headers=x_headers("POST", url, credentials),
                json=payload,
            )
        except ProviderError as error:
            if error.status == 403:
                raise ProviderError(
                    "X (Twitter) denied publishing (HTTP 403). Check Read and Write permissions in your "
                    "X (Twitter) developer app, then regenerate the Access Token and Access Token Secret "
                    "and reconnect X (Twitter). Also check API access and account restrictions.",
                    status=403,
                ) from None
            raise
        post_id = str(result.get("data", {}).get("id", ""))
        if not regex.fullmatch(r"[0-9]+", post_id):
            raise ProviderError(
                "X (Twitter) returned no reliable post receipt. Check X (Twitter) before retrying.",
                uncertain=True,
            )
        return {
            "status": "published",
            "remoteId": post_id,
            "url": "https://x.com/i/status/" + post_id,
        }
