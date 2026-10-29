// Public instructions for connecting tools to LightPost. Never place a real API key in this file:
// every example uses YOUR_API_KEY, and the real key is shown in Settings → Automation.
export const toolGuides = [
  {
    id: 'tool-internet',
    name: 'Reach LightPost from the internet',
    summary: 'For Claude on the web, Grok bots and cloud tools. Optional, off by default.',
    intro:
      'LightPost only listens on your own computer. Tools running on the internet cannot reach it until you give it a public address. Tools on the same computer (Claude Code, scripts, a local n8n) do not need this. Whatever carries the traffic can read it, and requests from outside are served only for the API and MCP endpoints with your API key.',
    steps: [
      {
        title: 'Pick an option',
        text: 'Open Settings → Reach it from the internet. **ngrok** keeps one address if you claim a free static domain, and LightPost runs the ngrok program you installed. **My own https address** is for Tailscale Funnel, a Cloudflare named tunnel or your own reverse proxy. Nothing is opened until you save a choice.',
        images: [
          {
            src: '/images/tools/internet-options.png',
            alt: 'LightPost Settings, Reach it from the internet, showing three choices: Off, ngrok and My own https address.',
          },
        ],
      },
      {
        title: 'ngrok',
        text: 'Create a free account at [ngrok.com](https://ngrok.com). Install ngrok **on the same system where LightPost runs**. On Windows with WSL that means the Linux version inside Ubuntu, not the Windows one. Run the `ngrok config add-authtoken` command ngrok shows you, then in Domains claim your free static domain. In LightPost choose ngrok, enter the domain, and Save. If ngrok is already set up you can leave every field empty.',
        code: 'ngrok version\nngrok config add-authtoken YOUR_NGROK_TOKEN',
        images: [
          {
            src: '/images/tools/internet-ngrok.png',
            alt: 'The ngrok choice in LightPost with optional static domain, authtoken and configuration file fields.',
          },
        ],
      },
      {
        title: 'Your own https address',
        text: 'Point your tunnel or proxy at `http://127.0.0.1:3000` (use your own port if you changed it). In LightPost choose My own https address, enter the public `https://` address, and Save. LightPost does not start or check it, it only shows the addresses to give your tools. You can also set `PUBLIC_URL` in your .env file.',
        images: [
          {
            src: '/images/tools/internet-own.png',
            alt: 'The My own https address choice with a field for the public https address.',
          },
        ],
      },
      {
        title: 'Check that it is protected',
        text: 'Open `https://YOUR-ADDRESS/api/v1/accounts` in a browser. It should say the API key is missing. Open `https://YOUR-ADDRESS/` and it should be refused. If both do that, outsiders cannot use anything without your key.',
      },
    ],
    troubleshooting:
      '**ngrok stopped, with an error:** the free plan allows one ngrok session at a time. Close any other ngrok window and press Save again.\n**ngrok: command not found:** ngrok is not installed where LightPost runs. Install it in the same Ubuntu window.\n**A “Visit Site” warning page:** ngrok’s free plan shows it to browsers. Tools sending your key are not affected by it.\n**The address changed:** without a static domain, ngrok may give a new address each time. Enter your static domain in LightPost, or use your own https address.',
  },
  {
    id: 'tool-claude',
    name: 'Claude on the web',
    summary: 'Add LightPost as a custom connector so Claude can draft, schedule and edit posts.',
    intro:
      'Claude on the web cannot reach localhost, so first give LightPost a public address (see the guide above). Then connect it as a custom connector. LightPost uses an API key, not OAuth sign-in.',
    steps: [
      {
        title: 'Copy the connector address',
        text: 'In LightPost, open Settings → Reach it from the internet. When it shows Online, copy the **Public MCP connector address**. It looks like `https://your-address/mcp/<your key>` and the key is part of it, so treat it like a password. Never share it or paste it into chats.',
        images: [
          {
            src: '/images/tools/connector-address.png',
            alt: 'The Public MCP connector address field in LightPost, hidden until you press Show, with a Copy button.',
          },
        ],
      },
      {
        title: 'Add the connector in Claude',
        text: 'In Claude, open Settings → Connectors → Add custom connector. Name it LightPost and paste the address.',
      },
      {
        title: 'Choose No sign-in',
        text: 'Under Authentication choose **No sign-in**, which is for servers that use an API key. If you leave “Sign in now” selected, Claude looks for an OAuth service and fails with a sign-in registration error. Then add the connector.',
      },
      {
        title: 'Try it',
        text: 'Start a chat and ask: “List my LightPost accounts, then save a draft post for LinkedIn saying hello.” Claude saves drafts unless you ask it to schedule or publish. Open Posts in LightPost to see the draft.',
      },
    ],
    troubleshooting:
      '**Sign-in or OAuth error:** choose No sign-in, and use the address that ends with your key.\n**Cannot connect:** check that Settings shows Online, that the address starts with https, and that API and MCP access is switched on.\n**Key leaked or shared:** Settings → Automation → Regenerate, then update the connector.\n**Publishing:** posts are public and cannot be undone. Ask Claude to schedule or publish only when you mean it. Posting to X needs you to accept its API cost.',
  },
  {
    id: 'tool-mcp',
    name: 'Claude Code and other MCP clients',
    summary: 'For tools on your own computer. No public address needed.',
    intro:
      'Any MCP client that supports streamable HTTP can use LightPost’s MCP server at `/mcp`. On the same computer you can use the local address. This guide’s commands follow each tool’s documentation and have not all been tried against every client.',
    steps: [
      {
        title: 'Get your key',
        text: 'Open Settings → Automation → API key and press Copy (or Show to read it). Press Regenerate for a new one. Or run `python -m lightpost api-key` in the LightPost folder.',
        images: [
          {
            src: '/images/tools/automation-panel.png',
            alt: 'Settings, API and MCP access, showing the hidden API key with Show, Copy and Regenerate buttons and the connect-a-tool addresses.',
          },
        ],
      },
      {
        title: 'Claude Code',
        text: 'Run this in a terminal, with your key in place of YOUR_API_KEY.',
        code: 'claude mcp add --transport http lightpost http://127.0.0.1:3000/mcp --header "Authorization: Bearer YOUR_API_KEY"',
      },
      {
        title: 'Other clients',
        text: 'Use the server address `http://127.0.0.1:3000/mcp` and send the header `Authorization: Bearer YOUR_API_KEY`. If a client cannot send headers, use the connector address `http://127.0.0.1:3000/mcp/YOUR_API_KEY` instead.',
      },
      {
        title: 'Available tools',
        text: 'list_accounts, create_post (create, edit, draft, schedule or publish, with images), get_post, list_posts, cancel_post, retry_post, delete_post and upload_image.',
      },
    ],
    troubleshooting:
      '**401 or “Missing or invalid API key”:** the key is wrong or has been regenerated.\n**403 “API access is turned off”:** switch on Allow API and MCP access in Settings → Automation.',
  },
  {
    id: 'tool-rest',
    name: 'n8n, scripts and other automation',
    summary: 'Use the REST API from n8n, Zapier-style tools, Grok bots or your own code.',
    intro:
      'The REST API lives at `/api/v1`, with a machine-readable description at `/api/v1/openapi.json`. Send your key as `Authorization: Bearer YOUR_API_KEY`. Use `http://127.0.0.1:3000` for tools on this computer, or your public address for tools on the internet. The curl examples below were tested. The tool-specific notes (n8n, Grok) were not.',
    steps: [
      {
        title: 'List your accounts',
        text: 'The ids, platforms and handles you can post to.',
        code: 'curl http://127.0.0.1:3000/api/v1/accounts \\\n  -H "Authorization: Bearer YOUR_API_KEY"',
      },
      {
        title: 'Save a draft',
        text: 'Drafts are the default and are never published until you say so.',
        code: 'curl -X POST http://127.0.0.1:3000/api/v1/posts \\\n  -H "Authorization: Bearer YOUR_API_KEY" -H "Content-Type: application/json" \\\n  -d \'{"text": "Hello from my automation", "accounts": ["linkedin"]}\'',
      },
      {
        title: 'Schedule a post',
        text: 'Add `scheduledAt`, with a timezone offset or a `timezone` name. Scheduling or publishing to X also needs `"xCostConfirmed": true`, because X bills API use.',
        code: '{"text": "See you then", "accounts": ["linkedin"],\n "scheduledAt": "2026-11-02T09:30:00", "timezone": "Asia/Jerusalem"}',
      },
      {
        title: 'Attach an image',
        text: 'Up to four JPEG, PNG or WebP images of 1 MB or less, each as a public https `url` or base64 `data`, with optional `alt` text.',
        code: '{"text": "With a picture", "accounts": ["all"],\n "images": [{"url": "https://example.com/pic.jpg", "alt": "A short description"}]}',
      },
      {
        title: 'Edit, cancel or retry',
        text: 'Send the same create request with `postId` to change a draft or scheduled post. Other endpoints are `POST /api/v1/posts/{id}/cancel` and `/retry`. Add an `idempotencyKey` so a repeated request returns the same post instead of a duplicate.',
      },
      {
        title: 'n8n and other tools',
        text: 'In n8n use an HTTP Request node: method POST, URL `…/api/v1/posts`, a header named Authorization with the value `Bearer YOUR_API_KEY`, and a JSON body. n8n and Grok bots running on the internet need a public address (see the first guide). A tool that supports MCP can use the MCP guides instead.',
      },
    ],
    troubleshooting:
      '**401:** missing or wrong key.\n**400 with a message:** the message says what to fix, for example a platform name that matches more than one account. Use the account id or handle.\n**Nothing was published:** posts without `scheduledAt` or `"status": "now"` stay drafts.',
  },
];
