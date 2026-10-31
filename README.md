# LightPost

**A lightweight system to automate posting to all of your socials from one dashboard.**

A lightweight, self-hosted social media scheduler. Connect your accounts, write a post, and publish now or schedule it for later—all from one dashboard.

![LightPost landing page with its publishing headline and social platform logos](public/landing.png)

## Features

- **Multi-account publishing:** connect multiple accounts, including several on the same platform, and post to them together.
- **Drafts and scheduling:** save, edit, duplicate, schedule, or publish immediately. Schedule inputs and post times use your browser’s timezone.
- **Per-account content:** choose whether each destination receives text, images, or both, within platform requirements.
- **YouTube videos:** connect your own channel, upload one MP4, choose visibility and audience, save drafts or schedule delivery. Resumable uploads and processing receipts survive restarts.
- **Images and previews:** import public JPEG URLs, reuse images from Saved Library, add alt text, and preview your content before posting.
- **Delivery tracking:** see individual account results and links to published posts. Retry unsuccessful destinations without repeating successful ones, or cancel further attempts.
- **Persistent queue:** posts survive restarts, overdue schedules resume, and supported media-processing progress is saved.
- **Account management:** verify connections, reconnect credentials, and disconnect accounts.
- **Built-in setup guides:** account eligibility, credential instructions, permissions, and troubleshooting for every platform.
- **Light and dark themes:** follow your device preference or save your own choice.
- **Your data, your server:** encrypted credentials, optional password protection, workspace settings, and post-history exports.

One Python process, SQLite, and plain HTML/CSS/JavaScript. No external database, frontend bundler, or AI dependency. Node.js is only needed for development checks and formatting.

## Supported platforms

| Platform                | Supported posts                                                                               |
| ----------------------- | --------------------------------------------------------------------------------------------- |
| Bluesky                 | Text and up to four images                                                                    |
| Mastodon                | Public text and up to four images                                                             |
| X (Twitter)             | Text and up to four JPEG/PNG images; API cost confirmation before publishing or scheduling    |
| Instagram               | One public JPEG with an optional caption; Creator/Business accounts                           |
| Threads                 | Text or one public JPEG                                                                       |
| Discord                 | Server webhook messages and up to four public images                                          |
| LinkedIn                | Public personal-profile posts with text and up to four images                                 |
| Telegram                | Public or private channel posts with text and up to four JPEG/PNG images                      |
| Facebook Pages          | Page feed posts with text and up to four JPEG/PNG images                                      |
| YouTube                 | One MP4 video with a title, optional description, visibility, audience and disclosure choices |
| TikTok                  | _Under development_                                                                           |
| Pinterest               | _Under development_                                                                           |
| Google Business Profile | _Under development_                                                                           |

Images are limited to **1 MB each**. Use **Add Image** to import a direct public HTTPS JPEG URL. Keep its URL available until delivery completes for Instagram, Threads, and Discord; your server can remain private. Platform-specific limits appear in the composer and Setup Guide. Video uploads are supported for YouTube only: one local MP4 up to 250 MB per post (H.264/AAC recommended). Create separate posts for YouTube videos and other platforms.

Bring your own authorized platform credentials. API fees, token expiry, permissions, and account eligibility still apply. YouTube includes a local Google OAuth connection using your own Desktop app client JSON; other platforms use credentials you obtain separately. For YouTube on a remote server, authorize locally and use Manual connection with a client ID, client secret and refresh token.

## Run locally or on your server

Requires **Python 3.11+ on Linux or macOS**.

```sh
git clone https://github.com/brainstormity/LightPost.git
cd LightPost
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m lightpost
```

Open [localhost:3000/app](http://localhost:3000/app), connect an account, and create your first post. New workspaces start empty. Open **Setup Guide** in the sidebar for platform-specific setup.

Keep the app running for scheduled delivery. For automatic startup on Linux, an optional systemd service is provided in [deploy/lightpost.service](deploy/lightpost.service); it expects the app at `/opt/lightpost`, a `lightpost` system user, and a writable `data/` directory.

## Configuration and backups

No `.env` file is required for the default setup. To override settings, copy [.env.example](.env.example) to `.env`. Environment variables take precedence.

| Setting              | Default / purpose                                                                 |
| -------------------- | --------------------------------------------------------------------------------- |
| `HOST`, `PORT`       | `127.0.0.1:3000`                                                                  |
| `DATABASE_PATH`      | `data/lightpost.sqlite`; media and the generated key live alongside it            |
| `ADMIN_PASSWORD`     | Optional locally; at least 16 characters required when binding beyond loopback    |
| `ENCRYPTION_KEY`     | Generated automatically if omitted; custom keys must be 64 hexadecimal characters |
| `COOKIE_SECURE`      | `false`; enable when serving over HTTPS                                           |
| `META_GRAPH_VERSION` | `v25.0` for Instagram and Facebook Pages                                          |

This is a single-owner application. Use a trusted network or HTTPS for remote access. Run one app process per database and keep SQLite on local storage.

For a full backup, stop the app and copy the **entire data directory and `.env`** together. Preserve the encryption key. Post-history exports omit credentials and image files, so they are not full backups. Credentials, databases, and keys are excluded from Git.

An interrupted delivery may need manual review if a platform accepted a post before the connection dropped. Check the destination before retrying; delivery across platforms is not atomic or guaranteed exactly once.

## Development

```sh
.venv/bin/python -m pip install -r requirements.txt ruff==0.14.2
npm ci
npm test
npm run build
npm run format
.venv/bin/python -m ruff check lightpost tests
```

Tests use temporary databases and mocked platform requests, never real social accounts. `npm run build` checks JavaScript syntax and runs frontend tests; there is no generated frontend to deploy. Run checks locally before pushing. No GitHub Actions workflows are included.

## License

[MIT](LICENSE). LightPost is an independent project and is not affiliated with Post Bridge or the supported platforms. The Instagram gradient glyph comes from [Meta’s official logo pack](https://www.meta.com/brand/resources/instagram/instagram-brand/). Additional logos come from [TikTok’s developer asset pack](https://developers.tiktok.com/doc/getting-started-design-guidelines), [Pinterest’s brand resources](https://business.pinterest.com/brand-guidelines/), and [Google Business Profile](https://business.google.com/us/business-profile/). Platform logos belong to their respective owners and are subject to their brand guidelines.
