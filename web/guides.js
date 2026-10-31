// Public setup instructions only. Never place account credentials in this file.
export const guides = [
  {
    id: 'youtube',
    name: 'YouTube',
    estimatedTime: '5–10 minutes',
    account: 'Your own Google account with a YouTube channel',
    steps: [
      {
        title: 'Create your Google Cloud project',
        text: 'Navigate to [Google Cloud Console](https://console.cloud.google.com/). Select the project picker → **New project**, enter a name such as LightPost YouTube, and create it. Select that project before continuing.\n*The project names and IDs in these screenshots are examples.*',
        images: [
          {
            src: '/images/youtube/create-project.png',
            alt: 'Google Cloud new project form with a LightPost YouTube test project name.',
          },
        ],
      },
      {
        title: 'Open APIs & Services',
        text: 'Click on **APIs & Services** and select **Library**.',
        images: [
          {
            src: '/images/youtube/select-project.png',
            alt: 'Google Cloud welcome page showing the APIs & Services shortcut.',
          },
        ],
      },
      {
        title: 'Enable YouTube Data API v3',
        text: 'Search for **YouTube Data API v3**, select it, and press **Enable**.\n*When enabled, its page shows Enabled and offers Manage or Disable API (as shown in the next step).*',
        images: [
          {
            src: '/images/youtube/api-library.png',
            alt: 'Google API Library search result for YouTube Data API v3.',
          },
        ],
      },
      {
        title: 'Open Google Auth Platform',
        text: 'Click on **OAuth consent screen** on the left. This opens Google Auth Platform. Click **Get started**.',
        images: [
          {
            src: '/images/youtube/api-enabled.png',
            alt: 'Enabled YouTube Data API page with OAuth consent screen in the left navigation.',
          },
        ],
      },
      {
        title: 'Complete the app setup',
        text: 'Now complete the app setup:',
        bullets: [
          'Enter your **app name**.',
          'Select your **support email**.',
          'Select **External** for a personal Gmail account.',
          'Add your **contact email**.',
          'Review the required policy acknowledgement.',
          'Complete setup.',
        ],
        images: [
          {
            src: '/images/youtube/oauth-setup.png',
            alt: 'Google Auth Platform setup with App Information, Audience, Contact Information and Finish steps.',
          },
        ],
      },
      {
        title: 'Open Audiences',
        text: 'Click on **Audiences** on the left side.',
        images: [
          {
            src: '/images/youtube/oauth-overview.png',
            alt: 'Google Auth Platform overview with Audience in the left navigation.',
          },
        ],
      },
      {
        title: 'Add yourself as a test user',
        text: 'Keep the app in **Testing**, choose **Add users** under **Test users**, enter the exact Google email you will use for your YouTube channel, and save.\n*Adding a test user permits sign-in; you must still grant YouTube permissions when connecting.*',
        images: [
          {
            src: '/images/youtube/test-users.png',
            alt: 'Google Auth Platform Audience page with the Add users panel open.',
          },
        ],
      },
      {
        title: 'Create a Desktop app client and download its JSON',
        text: 'Open **Clients → Create client**. Choose **Desktop app**, give it a name such as LightPost Local, and create it. **Download JSON** from the confirmation screen and keep it private. You will use this for connecting your YouTube account to LightPost.\n*Do not share screenshots containing client secrets or tokens.*',
        images: [
          {
            src: '/images/youtube/desktop-client.png',
            alt: 'Create OAuth client ID form with Desktop app selected.',
          },
        ],
      },
      {
        title: 'Connect YouTube in LightPost',
        text: 'Finally connect your YouTube account to LightPost by going to **Accounts → Connect account → YouTube**, choose the downloaded JSON, and press **Verify & connect**.\n*Sign in with your test-user email, choose the intended YouTube channel and grant both upload and read access.*',
        images: [],
      },
    ],
    connectionNote: {
      title: 'Keeping your YouTube account connected',
      paragraphs: [
        '**While your Google app is in Testing, you may need to approve access again after seven days.** If LightPost shows **Reconnect required**, click **Reconnect** and sign in with the Google account linked to your YouTube channel to renew authorization.',
        '**You do not need to create or download new JSON credentials.**',
        'For longer-lasting authorization, open [Google Auth Platform → Audience](https://console.cloud.google.com/auth/audience), select your project, and change the publishing status from **Testing** to **Production** by clicking **Publish app**. Then reconnect YouTube in LightPost to obtain fresh authorization. This removes the Testing-mode seven-day limit; access can still expire or be revoked for other reasons.',
      ],
    },
    troubleshooting:
      'Access blocked / developer-approved testers: add the exact sign-in email under Audience → Test users. Wrong channel: reconnect and choose the correct channel, or add it separately. Google rejects the JSON: use a Desktop app client rather than a Web application client. Expired authorization: reconnect, especially after seven days in Testing. Upload accepted but not visible: check processing, visibility and restrictions in YouTube Studio. A video locked private may require a YouTube API compliance audit; OAuth app verification is separate. Shorts eligibility is decided by YouTube; this integration uploads MP4 videos without a separate Shorts switch.',
  },
  {
    id: 'instagram',
    name: 'Instagram',
    estimatedTime: '5–10 minutes',
    account: 'An Instagram Creator or Business account',
    approval:
      'Use your own Instagram Creator or Business account and add it to your Meta app as an app-role/test account. Regular personal Instagram accounts cannot publish through this API.',
    credentials:
      'A long-lived Instagram Login user token with instagram_business_basic and instagram_business_content_publish. Not a Facebook Page token.',
    steps: [
      {
        title: 'Use a professional Instagram account',
        text: 'Use your own Creator or Business account. In Instagram settings, open Account type and tools → Switch to professional account. If your account is already professional, continue to the next step.',
        images: [
          {
            src: '/images/instagram/s1.png',
            alt: 'Instagram settings showing Switch to professional account.',
          },
        ],
      },
      {
        title: 'Register as a Meta developer',
        text: 'and register as a Meta developer by logging in with your Facebook account and verifying your email and phone.',
        link: {
          label: 'Meta developer registration',
          url: 'https://developers.facebook.com/async/registration/',
        },
        images: [],
      },
      {
        title: 'Create a Meta developer app',
        text: 'and choose My Apps → Create App. Enter an app name and contact email to continue.',
        images: [
          {
            src: '/images/instagram/s2.png',
            alt: 'Meta developer portal showing the Create App button.',
          },
        ],
        link: {
          label: 'Meta developer portal',
          url: 'https://developers.facebook.com/apps/',
        },
      },
      {
        title: 'Choose the Instagram use case',
        text: 'Under Content management, select Manage messaging & content on Instagram, then click Next.',
        images: [
          {
            src: '/images/instagram/s3.png',
            alt: 'Create an app with Manage messaging & content on Instagram selected.',
          },
        ],
      },
      {
        title: 'Continue without a business portfolio',
        text: 'For this own-account setup, select I don’t want to connect a business portfolio yet if that option is offered, then click Next. This guide uses your own account and app roles.',
        images: [
          {
            src: '/images/instagram/s4.png',
            alt: 'Business portfolio screen with the option to continue without connecting one.',
          },
        ],
      },
      {
        title: 'Review and create the app',
        text: 'Review the app details and Instagram use case, complete any requirements Meta displays, and click Create app. The app name in these screenshots is an example; use your own app.',
        images: [
          {
            src: '/images/instagram/s5.png',
            alt: 'Meta app overview with the Instagram use case and Create app button.',
          },
        ],
      },
      {
        title: 'Open app roles',
        text: 'In your new app, open App roles → Roles and select Add People.',
        images: [
          {
            src: '/images/instagram/s6.png',
            alt: 'Meta App roles screen showing Add People.',
          },
        ],
      },
      {
        title: 'Invite your Instagram account',
        text: 'Select Instagram Tester under Additional roles for this app. Enter your own Instagram username and click Add to send the invitation. Use the account you want LightPost to publish to.',
        images: [
          {
            src: '/images/instagram/s7.png',
            alt: 'Add people dialog with the Instagram Tester role selected.',
          },
        ],
      },
      {
        title: 'Find Instagram website permissions',
        text: 'Sign in to the Instagram account you invited. Open Settings → App website permissions → Apps and websites. Menu labels can vary between web and mobile.',
        images: [
          {
            src: '/images/instagram/s8.png',
            alt: 'Instagram App website permissions screen showing Apps and websites.',
          },
        ],
      },
      {
        title: 'Accept the tester invitation',
        text: 'Open the Tester Invites tab, find your Meta app, and click Accept. Sending the invitation in Meta alone is not enough.',
        images: [
          {
            src: '/images/instagram/s9.png',
            alt: 'Instagram Tester Invites tab showing the Accept button.',
          },
        ],
      },
      {
        title: 'Customize the Instagram use case',
        text: 'Return to your app in the Meta developer portal. Open Use cases and click Customize beside Manage messaging & content on Instagram.',
        images: [
          {
            src: '/images/instagram/s10.png',
            alt: 'Meta Use cases screen with Customize beside the Instagram use case.',
          },
        ],
      },
      {
        title: 'Use Instagram Login setup',
        text: 'Select API setup with Instagram login. The setup screen shown includes instagram_business_basic and instagram_business_manage_messages.',
        images: [
          {
            src: '/images/instagram/s11.png',
            alt: 'Instagram API setup with Instagram login selected and the Generate access tokens section below.',
          },
        ],
      },
      {
        title: 'Generate a token for your account',
        text: 'Expand Generate access tokens. If needed, use Add account to add the Instagram account whose tester invitation you accepted. Click Generate token beside that account. Webhook subscription can remain off for LightPost publishing.',
        images: [
          {
            src: '/images/instagram/s12.png',
            alt: 'Generate access tokens section showing an added Instagram account and Generate token.',
          },
        ],
      },
      {
        title: 'Authorize publishing access',
        text: 'When the Instagram popup appears, log in to the same professional account you added as a tester. Review the requested access, ensure Access and publish content is enabled, then click Allow. LightPost needs profile/basic access and content publishing; comment, message, and insight features are not used by this app.',
        images: [
          {
            src: '/images/instagram/s14.png',
            alt: 'Instagram consent popup with Access and publish content and the Allow button.',
          },
        ],
      },
      {
        title: 'Copy and keep the token private',
        text: 'In Token generated, select I understand, then Copy the token before closing the dialog. Use a long-lived Instagram Login user token; if your token is short-lived, follow Meta’s documented exchange before connecting it. LightPost does not perform the initial consent or token exchange. Never share the token in screenshots or commit it to Git.',
        images: [
          {
            src: '/images/instagram/s15.png',
            alt: 'Token generated dialog with a masked access token and Copy button.',
          },
        ],
      },
      {
        title: 'Connect your account in LightPost',
        text: 'Open LightPost → Accounts → Connect account → Instagram. Paste the long-lived Instagram user token and select Verify & connect. Use an Instagram Login token, not a Facebook Page token. Your credentials are stored encrypted.',
        images: [],
      },
    ],
    limits:
      'One JPEG at a direct public HTTPS URL, up to 1 MB, with a caption up to 2,200 characters. Use Add Image in the composer. The URL must remain available until publishing finishes; your server can remain private. No Reels, Stories or carousels in this version.',
    troubleshooting:
      'Check account eligibility, app-role invitation acceptance, token type and scopes. Meta must be able to fetch the JPEG without a login or redirect. Reconnect expired or revoked tokens.',
  },
  {
    id: 'x',
    name: 'X (Twitter)',
    estimatedTime: '3–5 minutes',
    account: 'Your own X (Twitter) account plus an X (Twitter) developer application',
    costNotice:
      'X API is not free. You must buy API credits in the X Developer Console to use it with LightPost.',
    steps: [
      {
        title: 'Open the console and buy API credits',
        text: 'and sign in with the X account you want to publish from. Complete developer registration if prompted. Open Billing → Credits to buy API credits, check your balance, and review spending limits before using the API. The amounts shown in this example are not a required purchase amount.',
        link: {
          label: 'X Developer Console',
          url: 'https://console.x.com/',
        },
        images: [
          {
            src: '/images/x/s1.png',
            alt: 'X Developer Console dashboard showing credit balances and the Billing Credits menu.',
          },
        ],
      },
      {
        title: 'Create your developer app',
        text: 'Open Apps → Create App, or select New App from the dashboard. Name your app and complete the details requested by X. Check that the app has access to your pay-per-use project.',
        images: [
          {
            src: '/images/x/s2.png',
            alt: 'X Developer Console Apps page showing Create App.',
          },
        ],
      },
      {
        title: 'Enable Read and Write',
        text: 'Open your app’s Settings → Authentication settings. Under App permissions, select Read and write for OAuth 1.0a and save the settings. Complete any required app information. Enable write access before generating your user tokens.',
        images: [
          {
            src: '/images/x/s3.png',
            alt: 'X app authentication settings with Read and write selected.',
          },
        ],
      },
      {
        title: 'Generate the OAuth 1.0 credentials',
        text: 'Return to Keys & Tokens → OAuth 1.0 Keys. Copy or regenerate the Consumer Key and Consumer Secret, then generate or regenerate the Access Token and Access Token Secret after enabling Read and write. Keep all four credentials private and use keys from the same app. The user token pair must belong to the account you want to post from. *The app-only Bearer Token and OAuth 2.0 Client Secret are not substitutes.*',
        images: [
          {
            src: '/images/x/s4.png',
            alt: 'X Keys and Tokens page showing OAuth 1.0 Keys, a masked Consumer Key, and the Access Token with Read and write access.',
          },
        ],
      },
      {
        title: 'Connect in LightPost',
        text: 'In LightPost → Accounts → Connect account → X (Twitter), enter the Consumer Key, Consumer Secret, Access Token, and Access Token Secret, then select Verify & connect. Account verification can consume API credits. LightPost asks you to confirm an estimated API cost before scheduling or publishing.',
        images: [],
      },
    ],
    limits:
      'Up to 280 weighted characters and four JPEG/PNG images, 1 MB each. Images upload directly and are attached as media IDs; their source URLs are never appended to the caption. The $0.015 post-create estimate excludes other API operations; posts containing a caption URL are listed at $0.20. Check current charges in the X (Twitter) Developer Console. An app-only bearer token cannot publish. Another account needs its own authorized user token pair; LightPost does not provide that OAuth consent flow.',
    troubleshooting:
      'If reads work but posting fails, verify write access and regenerate the user tokens after changing permissions. Check credits and account restrictions too.',
  },
  {
    id: 'discord',
    titles: [
      'Create a webhook',
      'Choose your channel',
      'Connect in LightPost',
      'Add more channels',
    ],
    name: 'Discord',
    estimatedTime: '30 seconds',
    account: 'A server where you can manage webhooks',
    steps: [
      {
        title: 'Create a webhook',
        text: 'Open Discord → Server Settings → Integrations → Webhooks → Create Webhook.',
        images: [
          {
            src: '/images/discord/s1.png',
            alt: 'Discord Server Settings showing Integrations → Webhooks and the Create Webhook button.',
          },
        ],
      },
      {
        title: 'Choose your channel',
        text: 'Choose the text channel, give the webhook a name, and select Copy Webhook URL.',
        images: [
          {
            src: '/images/discord/s2.png',
            alt: 'Discord webhook settings showing the Name field, Channel selector, and Copy Webhook URL button.',
          },
        ],
      },
      'In LightPost → Accounts → Connect account → Discord, paste the URL and select Verify & connect.',
      'Create a separate webhook for each channel you want to post to. Select several destinations in the composer if needed.',
    ],
    limits:
      'Posts use Discord Components V2 with text and an image gallery in one card. Text up to 2,000 characters and four images imported from public URLs, up to 1 MB each. Discord loads gallery images directly from their original URLs, so keep those URLs available. Mentions do not ping users, roles or everyone. Use a regular text channel; an existing thread can be targeted with thread_id in the URL.',
    troubleshooting:
      'If verification fails, check that the webhook still exists and that the URL was copied in full. Treat the URL as a password. Recreate it if it leaks.',
  },
  {
    id: 'telegram',
    name: 'Telegram',
    account: 'A public or private Telegram channel you manage',
    titles: [
      'Create a bot',
      'Grant channel posting permission',
      'Choose the destination',
      'Connect in LightPost',
    ],
    steps: [
      'Open the verified **@BotFather** account in Telegram. Send **/newbot**, follow its instructions and copy the **bot token** it gives you.',
      'Open your channel → Manage/Edit channel → Administrators → Add administrator. Add your new bot and enable **Post Messages**. Menu names vary between Telegram clients.',
      'For a public channel, use its **@username**. For a private channel, open the channel in [Telegram Web](https://web.telegram.org/k/) and copy the numeric value after **#** in the URL. If it does not already begin with **-100**, remove its leading minus sign and put **-100** before the remaining digits. Do not add the prefix twice.\nExample: **https://web.telegram.org/k/#-4528267057** contains **-4528267057**, so enter **-1004528267057** as the **channel ID**.\nAn invite link such as **t.me/+…** cannot be used as a channel ID.',
      'In LightPost → Accounts → Connect account → Telegram, enter the **channel ID or @username** and **bot token**, then choose **Verify & connect**. This checks the bot and its permissions without posting a test message. The same bot can connect to several channels separately.',
    ],
    limits:
      'Channel posts only. Text up to 4,096 characters, or up to four JPEG/PNG images with one caption up to 1,024 characters. Images upload directly; no public image URL is needed. Groups, direct messages, videos and custom formatting are not supported in this version.',
    troubleshooting:
      'If verification fails, check the bot token and channel ID, and make sure the bot is an administrator with Post Messages enabled. Reconnect if a token is revoked. Private-channel post links work only for channel members. If delivery is uncertain, check Telegram before retrying because a second attempt may create another post.',
  },
  {
    id: 'bluesky',
    titles: ['Open app passwords', 'Create a LightPost password', 'Enter your credentials'],
    name: 'Bluesky',
    estimatedTime: '30 seconds',
    account: 'Your own Bluesky account',
    steps: [
      'Sign into Bluesky and open Settings → Privacy and security → App passwords. Settings labels may vary between clients.',
      'Create an app password named LightPost and copy it before closing the dialog.',
      'In LightPost → Accounts → Connect account → Bluesky, enter your handle and the app password.\n*Leave Home server blank for accounts hosted on bsky.social. For a different host, enter your actual public HTTPS personal data server (PDS) address.*\n*Select Verify & connect. Repeat with the credentials for each additional account.*',
    ],
    limits:
      'Text up to 300 graphemes and four local images, up to 1 MB each. The app refreshes its session as it posts.',
    troubleshooting:
      'Use an app password without adding spaces, and check the handle and home server. If the app password was revoked, create another and reconnect the same account.',
  },
  {
    id: 'mastodon',
    titles: [
      'Create an application',
      'Set publishing permissions',
      'Copy your access token',
      'Connect in LightPost',
    ],
    name: 'Mastodon',
    estimatedTime: '1 minute',
    account: 'Your own account on a public Mastodon instance',
    steps: [
      'In your instance’s web interface, open Settings → Development → New application.',
      'Name the application LightPost and enable **read:accounts**, **write:statuses** and **write:media**.',
      'Save the application and copy the access token shown for your account.',
      'In LightPost → Accounts → Connect account → Mastodon, enter the instance URL and access token.\n\n*Select Verify & connect. Each account needs its own token, even on the same instance.*',
    ],
    limits:
      'Public text posts, conservatively limited to 500 characters, and four images up to 1 MB each. Instance-specific restrictions still apply.',
    troubleshooting:
      'Use the instance’s base HTTPS URL, without a path or port. Missing **write:media** permission can allow text while rejecting image posts.',
  },
  {
    id: 'facebook',
    name: 'Facebook Pages',
    account: 'A Facebook Page where you can create content',
    approval:
      'Use the Manage everything on your Page use case in a Meta developer app. For your own Page, check Standard Access and your app role. Advanced Access and App Review can be required for people outside the app’s roles. Personal profiles are not supported.',
    credentials:
      'Your numeric Page ID and that Page’s access token. If you already have both, use Connect Facebook Pages above. Keep your Instagram token in its existing account connection.',
    titles: [
      'Choose your Page',
      'Enable the Pages use case',
      'Authorize the permissions',
      'Get the Page token',
      'Extend and verify the Page token',
      'Connect and test',
    ],
    steps: [
      'Create a [Facebook Page](https://www.facebook.com/pages/creation/), or use one where you have full control or permission to create content. Your personal profile is not a Page.',
      'Open [Meta for Developers](https://developers.facebook.com/apps/) and select your existing app, or create an app. Add the Manage everything on your Page use case. Add pages_manage_posts and its dependencies pages_read_engagement and pages_show_list where available in the app dashboard.',
      'Open [Graph API Explorer](https://developers.facebook.com/tools/explorer/), select the same Meta app, and generate a User Access Token requesting those Page permissions. During authorization, select the Facebook Page you want to manage. If you change permissions, generate a new token; adding them in the dashboard does not update an old token.',
      'In [Graph API Explorer](https://developers.facebook.com/tools/explorer/), run GET me/accounts?fields=id,name,access_token,tasks with the User token. Find your Page and copy its id and access_token. The token inside the Page entry is the Page access token. An empty data list can mean no Page is managed by this account, missing permissions, or that the Page was not selected during authorization.',
      'Open [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/), paste the **Page access token**, and click **Debug**. Scroll down and click Extend Access Token if that option is available. Copy the resulting Page token, debug it again, and confirm **Type: Page** and **Expires: Never** before saving it in LightPost.\n\n*If the token still has an expiry date, extend the User token in the debugger, then use that long-lived User token in Graph API Explorer to request the Page token again with GET me/accounts?fields=id,name,access_token,tasks. Check the new Page token in the debugger. Never means no scheduled expiry; permission or account-security changes can still invalidate it.*',
      'In LightPost → Accounts → Connect account → Facebook Pages, enter the Page ID and Page access token. Verify & connect checks the Page identity without publishing a test post.',
    ],
    limits:
      'Page feed posts only: text up to the app’s 10,000-character limit, and up to four JPEG/PNG images. Images upload directly from this server; public image URLs are not required. Videos, Reels, Stories, groups and personal-profile posting are not included.',
    troubleshooting:
      'A connection verifies Page identity; publishing still needs the correct Page permissions. If Meta rejects a post, check the token’s granted permissions and your Page content access. For reliable scheduling, derive the Page token from a long-lived User token and check expiration in Meta’s [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/). LightPost does not extend Facebook tokens automatically. Tokens can expire or be revoked; use Reconnect to replace them. Check the Page before retrying an uncertain delivery.',
  },
  {
    id: 'threads',
    titles: [
      'Create your Meta app',
      'Add and authorize your own account',
      'Get a long-lived token',
      'Connect in LightPost',
    ],
    name: 'Threads',
    account: 'Your own Threads account',
    approval:
      'Use your own Threads account and add it as a tester or app-role user in your Meta app. An Instagram Business account is not required.',
    credentials: 'A long-lived Threads user token with threads_basic and threads_content_publish.',
    steps: [
      'Create a [Meta developer app](https://developers.facebook.com/apps/) with the Threads API use case.',
      'In your [Meta app](https://developers.facebook.com/apps/), add your Threads account as a tester or app-role user. Open [Threads](https://www.threads.com/) to accept the invitation and authorize the requested scopes.',
      'Use the [Threads developer setup](https://developers.facebook.com/docs/threads/) and [long-lived token instructions](https://developers.facebook.com/docs/threads/get-started/long-lived-tokens/) to generate and exchange a user token for a long-lived token.',
      'In LightPost → Accounts → Connect account → Threads, paste the token and verify the connection.',
    ],
    limits:
      'Text up to 500 characters, or one public HTTPS JPEG up to 1 MB. Use Add Image for the image. LightPost waits for a container’s processing to finish before confirming publication.',
    troubleshooting:
      'Use a Threads token, not an Instagram token. Check tester authorization and both scopes. LightPost refreshes connected long-lived Meta tokens after seven days when used; expired or revoked tokens require reconnecting.',
  },
  {
    id: 'linkedin',
    name: 'LinkedIn',
    account: 'Your personal LinkedIn profile',
    approval:
      'Create a [LinkedIn developer app](https://www.linkedin.com/developers/apps). LinkedIn may ask you to associate and verify a LinkedIn Page for the app. Posting here uses your personal profile; organization Pages need separate permissions and are not supported.',
    credentials:
      'One member access token with openid, profile and w_member_social. Use LinkedIn’s [official token generator](https://www.linkedin.com/developers/tools/oauth/token-generator); no client secret needs to be pasted into LightPost.',
    titles: [
      'Create an app',
      'Enable both products',
      'Generate a member token',
      'Connect your profile',
    ],
    steps: [
      'Open the [LinkedIn developer portal](https://developer.linkedin.com/), sign in, then open [My apps](https://www.linkedin.com/developers/apps) → Create app. Complete the application details and Page verification if LinkedIn requests it.',
      'In your [developer app](https://www.linkedin.com/developers/apps), open Products and request Share on LinkedIn and Sign In with LinkedIn using OpenID Connect. Wait until both products are available and Auth lists openid, profile and w_member_social.',
      'From the signed-in developer portal, open Docs and tools → [Token Generator](https://www.linkedin.com/developers/tools/oauth/token-generator). Choose your app, Member authorization code (3-legged), and the openid, profile and w_member_social scopes. Sign in as the member who will publish, approve access, then copy the access token.',
      'In LightPost → Accounts → Connect account → LinkedIn, paste the Member access token and choose Verify & connect. Repeat for another personal profile with that member’s own authorized token.',
    ],
    limits:
      'Public personal-profile posts with text up to 3,000 characters and up to four images, 1 MB each. Images upload directly to LinkedIn; a public image URL is not required. Links can be included in the text. Company Pages and videos are not supported.',
    troubleshooting:
      'If account verification fails, check openid and profile. If verification works but publishing fails, check w_member_social and the Share on LinkedIn product. Tokens expire or can be revoked; use the [Token Generator](https://www.linkedin.com/developers/tools/oauth/token-generator) again and Reconnect to replace the saved token. Self-service access tokens are not automatically refreshed by LightPost.',
  },
];
