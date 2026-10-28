import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import vm from 'node:vm';
import { guides } from '../web/guides.js';
import { toolGuides } from '../web/tool-guides.js';

// Exercise delegated dialog actions without a browser or social account.
test('dialogs, retry actions, and cancellation controls', async () => {
  const handlers = {};
  const windowHandlers = {};
  const modal = {
    innerHTML: '',
    open: false,
    addEventListener() {},
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
  };
  const fields = { innerHTML: '' };
  const platformField = { value: 'x' };
  const pageElement = { innerHTML: '' };
  const postListElement = { innerHTML: '' };
  const pagination = { innerHTML: '' };
  const document = {
    querySelector(selector) {
      return (
        {
          '#root': {},
          '#modal': modal,
          '#platform': platformField,
          '#page': pageElement,
          '#connect-fields': fields,
          '#post-list': postListElement,
          '#pagination': pagination,
        }[selector] || null
      );
    },
    querySelectorAll() {
      return [];
    },
    addEventListener(type, callback) {
      (handlers[type] ||= []).push(callback);
    },
  };
  const context = vm.createContext({
    document,
    guides,
    toolGuides,
    window: {
      addEventListener(type, callback) {
        windowHandlers[type] = callback;
      },
    },
    location: { hash: '', pathname: '/app' },
    setInterval() {},
    setTimeout() {},
    clearTimeout() {},
    Intl,
    console,
  });
  const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8')
    .replace(/^import .*\n/gm, '')
    .replace(/\ninit\(\);\s*$/, '');
  vm.runInContext(source, context);
  vm.runInContext(
    "state = {accounts: [], media: [], platforms: [{id: 'x', name: 'X', help: 'X setup'}]};",
    context,
  );
  for (const [action, expected] of [
    ['source-image', 'Direct JPEG URL'],
    ['library', 'Saved Library'],
    ['connect', 'Connect an account'],
  ]) {
    const button = { dataset: { action }, type: 'button', innerHTML: 'Open', isConnected: true };
    for (const handler of handlers.click) await handler({ target: { closest: () => button } });
    assert.equal(modal.open, true);
    assert.ok(modal.innerHTML.includes(expected), action);
    if (action === 'connect') assert.ok(fields.innerHTML.includes('Consumer Key (OAuth 1.0)'));
    modal.close();
  }
  vm.runInContext(
    `state.accounts = [{id: 'saved-x', platform: 'x'}];
     api = async () => ({values: {}, savedFields: ['apiKey', 'apiSecret', 'secret', 'tokenSecret']});`,
    context,
  );
  await vm.runInContext("connectDialog('saved-x')", context);
  assert.ok(modal.innerHTML.includes('Reconnect account'));
  assert.ok(fields.innerHTML.includes('Your saved credentials are ready to use.'));
  assert.equal((fields.innerHTML.match(/ · Saved/g) || []).length, 4);
  assert.equal((fields.innerHTML.match(/value=""/g) || []).length, 4);
  assert.ok(!fields.innerHTML.includes(' required'));
  platformField.value = 'linkedin';
  vm.runInContext(
    `state.platforms.push({id: 'linkedin', name: 'LinkedIn', help: 'LinkedIn setup'}); renderConnectFields();`,
    context,
  );
  assert.ok(fields.innerHTML.includes('Member access token'));
  assert.ok(fields.innerHTML.includes('https://developer.linkedin.com/'));
  assert.ok(!fields.innerHTML.includes('Consumer Key'));
  assert.equal((fields.innerHTML.match(/type="password"/g) || []).length, 1);
  platformField.value = 'telegram';
  vm.runInContext(
    `state.platforms.push({id: 'telegram', name: 'Telegram', help: 'Telegram setup'}); renderConnectFields();`,
    context,
  );
  assert.ok(fields.innerHTML.includes('Channel username or ID'));
  assert.ok(fields.innerHTML.includes('Bot token from BotFather'));
  assert.ok(!fields.innerHTML.includes('Consumer Key'));
  vm.runInContext(
    `renderConnectFields({values: {chatId: '-1001234567890'}, savedFields: ['chatId', 'secret']});`,
    context,
  );
  assert.ok(fields.innerHTML.includes('value="-1001234567890"'));
  assert.ok(fields.innerHTML.includes('Bot token from BotFather · Saved'));
  assert.ok(!fields.innerHTML.includes(' required'));
  platformField.value = 'facebook';
  vm.runInContext(
    `state.platforms.push({id: 'facebook', name: 'Facebook Pages', help: 'Facebook setup'}); renderConnectFields();`,
    context,
  );
  assert.ok(fields.innerHTML.includes('Facebook Page ID'));
  assert.ok(fields.innerHTML.includes('Page access token'));
  assert.ok(!fields.innerHTML.includes('Consumer Key'));
  vm.runInContext(
    `renderConnectFields({values: {pageId: '1234567890'}, savedFields: ['pageId', 'secret']});`,
    context,
  );
  assert.ok(fields.innerHTML.includes('value="1234567890"'));
  assert.ok(fields.innerHTML.includes('Page access token · Saved'));
  assert.ok(!fields.innerHTML.includes(' required'));
  platformField.value = 'x';
  for (const guide of guides) {
    vm.runInContext(
      `location.hash = ${JSON.stringify('#docs/' + guide.id)}; renderDocs();`,
      context,
    );
    const html = pageElement.innerHTML;
    const button = `data-connect-platform="${guide.id}"`;
    assert.equal(html.split(button).length - 1, 1);
    assert.ok(html.indexOf(button) < html.indexOf('<article class="docs-article">'));
  }
  vm.runInContext("location.hash = '#docs'; renderDocs();", context);
  assert.ok(pageElement.innerHTML.includes('<h1>Setup Guide</h1>'));
  for (const guide of toolGuides) {
    assert.ok(pageElement.innerHTML.includes(`href="#docs/${guide.id}"`), guide.id);
  }
  for (const guide of toolGuides) {
    vm.runInContext(
      `location.hash = ${JSON.stringify('#docs/' + guide.id)}; renderDocs();`,
      context,
    );
    const html = pageElement.innerHTML;
    assert.ok(html.includes(`<h1>${guide.name}</h1>`), guide.id);
    assert.equal(html.split('class="docs-section docs-step"').length - 1, guide.steps.length);
    for (const image of guide.steps.flatMap((step) => step.images || [])) {
      assert.ok(existsSync(new URL(`../web${image.src}`, import.meta.url)), image.src);
      assert.ok(image.alt.length > 0, image.src);
    }
    // Only placeholders, never a real key.
    assert.ok(!/lp_[A-Za-z0-9_-]{20,}/.test(html), guide.id);
  }
  vm.runInContext("location.hash = '#docs'; renderDocs();", context);

  const retryIds = vm.runInContext(
    `retryAccountIds({
    accountIds: ['successful-x', 'failed-x', 'instagram', 'demo', 'unattempted'],
    results: [
      {accountId: 'successful-x', status: 'failed'},
      {accountId: 'successful-x', status: 'published'},
      {accountId: 'failed-x', status: 'failed'},
      {accountId: 'instagram', status: 'uncertain'},
      {accountId: 'demo', status: 'simulated'},
    ],
  })`,
    context,
  );
  assert.deepEqual(Array.from(retryIds), ['failed-x', 'instagram', 'unattempted']);
  vm.runInContext(
    `state.accounts = [];
    postList = [{id: 'processing-post', status: 'processing', text: 'Test', accountIds: []}];
    postTotal = 1; filter = 'all'; renderPostList();`,
    context,
  );
  assert.ok(postListElement.innerHTML.includes('data-cancel="processing-post"'));
  assert.ok(postListElement.innerHTML.includes('Cancel posting'));
  vm.runInContext(
    `state.accounts = [{id: 'x', platform: 'x', handle: '@test'}];
    postList = [{id: 'published-post', status: 'published', text: 'Test', accountIds: ['x'],
      results: [{accountId: 'x', status: 'published', message: 'Published text only. Images were omitted for X.'}]}];
    renderPostList();`,
    context,
  );
  assert.ok(!postListElement.innerHTML.includes('Images were omitted'));
  assert.ok(postListElement.innerHTML.includes('Published'));

  vm.runInContext(
    `state.accounts = [{id: 'successful-x', platform: 'x'}];
    postList = [{id: 'retry-post', status: 'needs_review', text: 'Test',
      accountIds: ['successful-x', 'instagram'],
      results: [{accountId: 'successful-x', status: 'published'}, {accountId: 'instagram', status: 'uncertain'}]}];
    const requests = [];
    api = async (path, options) => {requests.push({path, body: options.body}); return {status: 'cancelled'};};
    confirm = () => {throw Error('Unexpected confirmation');};
    reloadState = async () => {}; renderPosts = () => {}; toast = () => {};`,
    context,
  );
  const retryButton = {
    dataset: { retry: 'retry-post' },
    type: 'button',
    innerHTML: 'Retry',
    isConnected: true,
  };
  for (const handler of handlers.click) await handler({ target: { closest: () => retryButton } });
  const cancelButton = {
    dataset: { cancel: 'processing-post' },
    type: 'button',
    innerHTML: 'Cancel',
    isConnected: true,
  };
  vm.runInContext('confirm = () => false;', context);
  for (const handler of handlers.click) await handler({ target: { closest: () => cancelButton } });
  assert.equal(vm.runInContext('requests.length', context), 1);
  vm.runInContext('confirm = () => true;', context);
  for (const handler of handlers.click) await handler({ target: { closest: () => cancelButton } });
  const requests = JSON.parse(vm.runInContext('JSON.stringify(requests)', context));
  assert.equal(requests.length, 2);
  assert.equal(requests[0].path, '/posts/retry-post/retry');
  assert.deepEqual(requests[0].body, { xCostConfirmed: false });
  assert.equal(requests[1].path, '/posts/processing-post/cancel');
  vm.runInContext(
    `const themeCalls = []; window.lightpostTheme = {current: () => 'light', toggle: () => themeCalls.push('toggle')};
    composer.text = 'Unsaved post';`,
    context,
  );
  const themeButton = { dataset: { action: 'toggle-theme' }, type: 'button' };
  for (const handler of handlers.click) await handler({ target: { closest: () => themeButton } });
  assert.equal(vm.runInContext('themeCalls.length', context), 1);
  assert.equal(vm.runInContext('composer.text', context), 'Unsaved post');
  assert.equal(vm.runInContext('requests.length', context), 2);
  vm.runInContext(
    `page = 'posts'; state.posts = [{status: 'queued'}];
    let refreshCount = 0; let renderCount = 0;
    reloadState = async () => {refreshCount++; state.posts = [{status: 'published'}];};
    renderPostList = () => {renderCount++;};
    modal.open = true;`,
    context,
  );
  await vm.runInContext('refreshPostStatus(true)', context);
  assert.equal(vm.runInContext('refreshCount', context), 1);
  assert.equal(vm.runInContext('state.posts[0].status', context), 'published');
  document.hidden = true;
  await vm.runInContext('refreshPostStatus(true)', context);
  assert.equal(vm.runInContext('refreshCount', context), 1);
  document.hidden = false;
  for (const handler of handlers.visibilitychange) await handler();
  // Allow the event's async refresh to finish.
  await vm.runInContext('Promise.resolve()', context);
  assert.equal(vm.runInContext('refreshCount', context), 2);
  await windowHandlers.focus();
  await vm.runInContext('Promise.resolve()', context);
  assert.equal(vm.runInContext('refreshCount', context), 3);
  assert.equal(vm.runInContext('renderCount', context), 3);
  assert.equal(vm.runInContext('composer.text', context), 'Unsaved post');
  vm.runInContext(
    `state.accounts = [{id: 'ig', platform: 'instagram'}];
     composer.accountIds = ['ig']; composer.contentOptions = {};`,
    context,
  );
  assert.equal(vm.runInContext('savedImageIssue({publicSource: true})', context), '');
  assert.match(
    vm.runInContext('savedImageIssue({publicSource: false})', context),
    /public JPEG URL/,
  );
  vm.runInContext(`composer.contentOptions.ig = {image: false};`, context);
  assert.equal(vm.runInContext('savedImageIssue({publicSource: false})', context), '');
  assert.equal(
    vm.runInContext('docsText("<script>Access Token Secret</script>")', context),
    '&lt;script&gt;<strong>Access Token Secret</strong>&lt;/script&gt;',
  );
  vm.runInContext('renderLanding()', context);
  const landing = vm.runInContext('root.innerHTML', context);
  assert.ok(landing.indexOf('</h1>') < landing.indexOf('class="hero-platforms"'));
  const logos = [...landing.matchAll(/src="(\/logos\/[^"]+)"/g)].map((match) => match[1]);
  assert.equal(logos.length, 12);
  assert.ok(!/under development/i.test(landing));
  for (const [platform, count] of [
    ['instagram', 14],
    ['x', 4],
  ]) {
    context.location.hash = `#docs/${platform}`;
    vm.runInContext('renderDocs()', context);
    const screenshots = guides
      .find((guide) => guide.id === platform)
      .steps.flatMap((step) => step.images);
    assert.equal(screenshots.length, count);
    for (const image of screenshots) {
      assert.ok(existsSync(new URL(`../web${image.src}`, import.meta.url)), image.src);
      assert.ok(pageElement.innerHTML.includes(`src="${image.src}"`), image.src);
      assert.ok(image.alt.length > 0, image.src);
    }
    if (platform === 'x') {
      for (const name of [
        'Consumer Key',
        'Consumer Secret',
        'Access Token',
        'Access Token Secret',
      ]) {
        assert.ok(pageElement.innerHTML.includes(`<strong>${name}</strong>`), name);
      }
      assert.ok(
        !pageElement.innerHTML.includes('Register for developer access with the X account'),
      );
      assert.ok(pageElement.innerHTML.includes('<h1><span class="platform-mark x"'));
      assert.ok(
        pageElement.innerHTML.includes(
          '<em>The app-only Bearer Token and OAuth 2.0 Client Secret are not substitutes.</em>',
        ),
      );
      assert.ok(pageElement.innerHTML.includes('X API is not free.'));
      assert.ok(pageElement.innerHTML.includes('You must buy API credits'));
      assert.ok(
        pageElement.innerHTML.indexOf('X API is not free.') <
          pageElement.innerHTML.indexOf('id="x-step-1"'),
      );
    }
  }

  context.location.hash = '#docs/telegram';
  vm.runInContext('renderDocs()', context);
  assert.ok(
    pageElement.innerHTML.includes(
      '<a href="https://web.telegram.org/k/" target="_blank" rel="noopener noreferrer">Telegram Web</a>',
    ),
  );
  assert.ok(!pageElement.innerHTML.includes('<strong>Telegram Web</strong>'));
  assert.ok(pageElement.innerHTML.includes('<strong>-1004528267057</strong>'));
  assert.ok(pageElement.innerHTML.includes('<strong>-100</strong>'));
  assert.ok(pageElement.innerHTML.includes('<strong>bot token</strong>'));
  assert.ok(!pageElement.innerHTML.includes('id="telegram-step-5"'));
  assert.ok(!pageElement.innerHTML.includes('No developer app registration or app review'));

  for (const path of logos) {
    assert.ok(existsSync(new URL(`../web${path}`, import.meta.url)), path);
    assert.ok(path.endsWith('.svg'), path);
  }
  for (const name of ['TikTok', 'Pinterest', 'Google Business Profile']) {
    assert.ok(landing.includes(`aria-label="${name}"`), name);
  }
});

test('multi-platform preview shows X images and follows its image toggle', () => {
  const elements = new Map();
  const document = {
    querySelector(selector) {
      if (!elements.has(selector))
        elements.set(selector, {
          innerHTML: '',
          addEventListener() {},
          classList: { toggle() {} },
        });
      return elements.get(selector);
    },
    querySelectorAll() {
      return [];
    },
    addEventListener() {},
  };
  const context = vm.createContext({
    document,
    guides,
    toolGuides,
    window: { addEventListener() {} },
    location: { hash: '', pathname: '/app' },
    setInterval() {},
    setTimeout() {},
    clearTimeout() {},
    Intl,
  });
  const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8')
    .replace(/^import .*\n/gm, '')
    .replace(/\ninit\(\);\s*$/, '');
  vm.runInContext(source, context);
  vm.runInContext(
    `
    readComposer = () => {};
    state = {accounts: ['x', 'telegram', 'bluesky', 'facebook'].map(platform => ({id: platform, platform, name: platform, handle: '@test'})),
      media: [{id: 'photo', alt: 'Test image'}], platforms: [], settings: {name: 'Test'}};
    composer.accountIds = state.accounts.map(account => account.id);
    composer.mediaIds = ['photo'];
    renderAttachments(); updatePreview();
  `,
    context,
  );
  const images = elements.get('#preview-images');
  assert.ok(images.innerHTML.includes('/api/media/photo'));
  assert.equal(images.hidden, false);
  vm.runInContext(`composer.contentOptions.x = {image: false}; updatePreview();`, context);
  assert.equal(images.hidden, true);
  vm.runInContext(`composer.contentOptions.x.image = true; updatePreview();`, context);
  assert.equal(images.hidden, false);
});
