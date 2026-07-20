#!/usr/bin/env node

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { buildSvelteLiveRootComponent } from './live/sveltekit-adapter.mjs';

const scriptsDir = path.dirname(fileURLToPath(import.meta.url));
const serverScript = path.join(scriptsDir, 'live-server.mjs');
const injectScript = path.join(scriptsDir, 'live-inject.mjs');

function runScript(script, args, cwd) {
  return execFileSync(process.execPath, [script, ...args], {
    cwd,
    encoding: 'utf-8',
    stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

async function postJson(url, body, origin = 'https://preview.example') {
  return fetch(url, {
    method: 'POST',
    headers: { Origin: origin, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function fetchAfterStartup(url, init) {
  let lastError;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      return await fetch(url, init);
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  }
  throw lastError;
}

async function readSseUntil(reader, predicate, timeoutMs = 1_000) {
  const decoder = new TextDecoder();
  let buffered = '';
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const waitMs = deadline - Date.now();
    const result = await Promise.race([
      reader.read(),
      new Promise((resolve) => setTimeout(() => resolve(null), waitMs)),
    ]);
    if (result === null || result.done) return null;
    buffered += decoder.decode(result.value, { stream: true });
    const frames = buffered.split('\n\n');
    buffered = frames.pop() || '';
    for (const frame of frames) {
      const data = frame.split('\n').find((line) => line.startsWith('data: '));
      if (!data) continue;
      const event = JSON.parse(data.slice('data: '.length));
      if (predicate(event)) return event;
    }
  }
  return null;
}

test('completed live sessions ignore late generation checkpoints', async () => {
  const projectRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-live-fence-'));
  fs.writeFileSync(path.join(projectRoot, 'index.html'), '<div>original</div>\n');
  let serverInfo = null;
  let reader = null;
  try {
    serverInfo = JSON.parse(runScript(serverScript, ['--background'], projectRoot));
    const baseUrl = `http://127.0.0.1:${serverInfo.port}`;
    const token = serverInfo.token;
    const id = 'deadbeef';
    const stream = await fetchAfterStartup(`${baseUrl}/events?token=${token}`, {
      headers: { Origin: 'https://preview.example' },
    });
    assert.equal(stream.status, 200);
    reader = stream.body.getReader();
    assert.equal(
      (await readSseUntil(reader, (event) => event.type === 'connected'))?.type,
      'connected',
    );

    const generate = await postJson(`${baseUrl}/events?token=${token}`, {
      token,
      type: 'generate',
      id,
      count: 1,
      action: 'polish',
      pageUrl: '/',
      element: { outerHTML: '<div>original</div>' },
    });
    assert.equal(generate.status, 200);
    const complete = await postJson(`${baseUrl}/poll`, {
      token,
      type: 'complete',
      id,
      sourceEventType: 'generate',
      file: 'index.html',
    });
    assert.equal(complete.status, 200);
    assert.equal(
      (await readSseUntil(reader, (event) => event.type === 'complete'))?.type,
      'complete',
    );

    await reader.cancel();
    const completedStream = await fetch(`${baseUrl}/events?token=${token}`, {
      headers: { Origin: 'https://preview.example' },
    });
    assert.equal(completedStream.status, 200);
    reader = completedStream.body.getReader();
    assert.equal(
      (await readSseUntil(reader, (event) => event.type === 'connected'))?.type,
      'connected',
    );

    const checkpoint = await postJson(`${baseUrl}/events?token=${token}`, {
      token,
      type: 'checkpoint',
      id,
      revision: 1,
      reason: 'variants_ready',
      phase: 'completed',
      arrivedVariants: 1,
      expectedVariants: 1,
      previewFile: 'index.html',
    });
    assert.equal(checkpoint.status, 200);
    assert.equal(
      await readSseUntil(reader, (event) => event.type === 'variant_progress', 250),
      null,
    );
  } finally {
    if (reader) await reader.cancel().catch(() => {});
    if (serverInfo) {
      try {
        runScript(serverScript, ['stop', '--keep-inject'], projectRoot);
      } catch {
        try { process.kill(serverInfo.pid, 'SIGTERM'); } catch {}
      }
    }
    fs.rmSync(projectRoot, { recursive: true, force: true });
  }
});

test('live mode protects its capability and project source boundary', async () => {
  const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-live-security-'));
  const projectRoot = path.join(fixtureRoot, 'project');
  const configDir = path.join(projectRoot, '.impeccable', 'live');
  fs.mkdirSync(configDir, { recursive: true });
  fs.writeFileSync(path.join(projectRoot, 'inside.txt'), 'inside-project\n');
  fs.writeFileSync(path.join(fixtureRoot, 'outside.txt'), 'outside-project\n');
  fs.symlinkSync('inside.txt', path.join(projectRoot, 'internal-link.txt'));
  fs.symlinkSync(path.join(fixtureRoot, 'outside.txt'), path.join(projectRoot, 'escaping-link.txt'));

  let serverInfo = null;
  try {
    serverInfo = JSON.parse(runScript(serverScript, ['--background'], projectRoot));
    const baseUrl = `http://127.0.0.1:${serverInfo.port}`;
    const validToken = serverInfo.token;
    const hostileOrigin = 'https://hostile.example';
    const previewOrigin = 'https://preview.example';

    const missingToken = await fetchAfterStartup(`${baseUrl}/live.js`, {
      headers: { Origin: hostileOrigin },
    });
    assert.equal(missingToken.status, 401);
    assert.equal(missingToken.headers.get('access-control-allow-origin'), null);
    assert.equal((await missingToken.text()).includes(validToken), false);

    const wrongToken = await fetch(`${baseUrl}/live.js?token=wrong`, {
      headers: { Origin: hostileOrigin },
    });
    assert.equal(wrongToken.status, 401);
    assert.equal(wrongToken.headers.get('access-control-allow-origin'), null);

    const bootstrap = await fetch(`${baseUrl}/live.js?token=${validToken}`, {
      headers: { Origin: previewOrigin },
    });
    assert.equal(bootstrap.status, 200);
    assert.equal(bootstrap.headers.get('access-control-allow-origin'), previewOrigin);
    assert.equal((await bootstrap.text()).includes(validToken), true);

    const deniedPreflight = await fetch(`${baseUrl}/source?path=inside.txt`, {
      method: 'OPTIONS',
      headers: { Origin: hostileOrigin, 'Access-Control-Request-Method': 'GET' },
    });
    assert.equal(deniedPreflight.status, 401);
    assert.equal(deniedPreflight.headers.get('access-control-allow-origin'), null);

    const allowedPreflight = await fetch(
      `${baseUrl}/source?token=${validToken}&path=inside.txt`,
      {
        method: 'OPTIONS',
        headers: { Origin: previewOrigin, 'Access-Control-Request-Method': 'GET' },
      },
    );
    assert.equal(allowedPreflight.status, 204);
    assert.equal(allowedPreflight.headers.get('access-control-allow-origin'), previewOrigin);

    for (const route of ['/events', '/manual-edit-stash']) {
      const preflight = await fetch(`${baseUrl}${route}?token=${validToken}`, {
        method: 'OPTIONS',
        headers: { Origin: previewOrigin, 'Access-Control-Request-Method': 'POST' },
      });
      assert.equal(preflight.status, 204, route);
      assert.equal(preflight.headers.get('access-control-allow-origin'), previewOrigin, route);
    }

    const eventPost = await fetch(`${baseUrl}/events?token=${validToken}`, {
      method: 'POST',
      headers: { Origin: previewOrigin, 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: validToken, type: 'exit' }),
    });
    assert.equal(eventPost.status, 200);
    assert.equal(eventPost.headers.get('access-control-allow-origin'), previewOrigin);

    const stashPost = await fetch(`${baseUrl}/manual-edit-stash?token=${validToken}`, {
      method: 'POST',
      headers: { Origin: previewOrigin, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token: validToken,
        id: 'deadbeef',
        pageUrl: '/test',
        element: {},
        ops: [{ ref: 'copy-1', tag: 'span', originalText: 'old', newText: 'new' }],
      }),
    });
    assert.equal(stashPost.status, 200);
    assert.equal(stashPost.headers.get('access-control-allow-origin'), previewOrigin);

    const sourceCases = [
      ['inside.txt', 200, 'inside-project'],
      ['internal-link.txt', 200, 'inside-project'],
      ['escaping-link.txt', 403, 'Forbidden'],
      ['/etc/hosts', 400, 'Bad path'],
      ['missing.txt', 404, 'File not found'],
    ];
    for (const [sourcePath, status, body] of sourceCases) {
      const response = await fetch(
        `${baseUrl}/source?token=${validToken}&path=${encodeURIComponent(sourcePath)}`,
      );
      assert.equal(response.status, status, sourcePath);
      assert.equal((await response.text()).trim(), body, sourcePath);
    }

    const completedSessionId = 'cafebabe';
    const generate = await fetch(`${baseUrl}/events?token=${validToken}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token: validToken,
        type: 'generate',
        id: completedSessionId,
        count: 3,
        action: 'impeccable',
        element: { outerHTML: '<section>preview</section>' },
      }),
    });
    assert.equal(generate.status, 200);
    const complete = await fetch(`${baseUrl}/poll`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token: validToken,
        type: 'complete',
        id: completedSessionId,
        file: 'inside.txt',
      }),
    });
    assert.equal(complete.status, 200);

    const eventsController = new AbortController();
    const events = await fetch(`${baseUrl}/events?token=${validToken}`, {
      signal: eventsController.signal,
    });
    assert.equal(events.status, 200);
    const eventReader = events.body.getReader();
    await eventReader.read();
    const lateCheckpoint = await fetch(`${baseUrl}/events?token=${validToken}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token: validToken,
        type: 'checkpoint',
        id: completedSessionId,
        revision: 1,
        reason: 'variants_progress',
        arrivedVariants: 1,
        expectedVariants: 3,
        previewFile: 'inside.txt',
      }),
    });
    assert.equal(lateCheckpoint.status, 200);
    const lateEvent = await Promise.race([
      eventReader.read().then(({ value }) => new TextDecoder().decode(value)),
      new Promise((resolve) => setTimeout(() => resolve(''), 250)),
    ]);
    eventsController.abort();
    assert.equal(lateEvent.includes('variant_progress'), false);

    const configPath = path.join(configDir, 'config.json');
    const tokenizedScript = `live.js?token=${encodeURIComponent(validToken)}`;

    fs.writeFileSync(path.join(projectRoot, 'index.html'), '<html><body>html</body></html>\n');
    fs.writeFileSync(configPath, JSON.stringify({
      files: ['index.html'],
      insertBefore: '</body>',
      commentSyntax: 'html',
    }));
    assert.throws(
      () => runScript(injectScript, ['--port', String(serverInfo.port)], projectRoot),
      /Command failed/,
    );
    runScript(injectScript, ['--port', String(serverInfo.port), '--token', validToken], projectRoot);
    assert.equal(
      fs.readFileSync(path.join(projectRoot, 'index.html'), 'utf-8').includes(tokenizedScript),
      true,
    );
    runScript(injectScript, ['--remove'], projectRoot);
    assert.equal(fs.readFileSync(path.join(projectRoot, 'index.html'), 'utf-8').includes('live.js'), false);

    fs.writeFileSync(path.join(projectRoot, 'index.astro'), '<html><body>astro</body></html>\n');
    fs.writeFileSync(configPath, JSON.stringify({
      files: ['index.astro'],
      insertBefore: '</body>',
      commentSyntax: 'html',
    }));
    runScript(injectScript, ['--port', String(serverInfo.port), '--token', validToken], projectRoot);
    const astro = fs.readFileSync(path.join(projectRoot, 'index.astro'), 'utf-8');
    assert.equal(astro.includes('script is:inline'), true);
    assert.equal(astro.includes(tokenizedScript), true);

    fs.writeFileSync(
      path.join(projectRoot, 'App.jsx'),
      'export default function App() { return (<body>jsx</body>); }\n',
    );
    fs.writeFileSync(configPath, JSON.stringify({
      files: ['App.jsx'],
      insertBefore: '</body>',
      commentSyntax: 'jsx',
    }));
    runScript(injectScript, ['--port', String(serverInfo.port), '--token', validToken], projectRoot);
    const jsx = fs.readFileSync(path.join(projectRoot, 'App.jsx'), 'utf-8');
    assert.equal(jsx.includes('{/* impeccable-live-start */}'), true);
    assert.equal(jsx.includes(tokenizedScript), true);

    const svelte = buildSvelteLiveRootComponent(serverInfo.port, validToken);
    assert.equal(svelte.includes(tokenizedScript), true);

    const liveBrowser = fs.readFileSync(path.join(scriptsDir, 'live-browser.js'), 'utf-8');
    assert.equal(liveBrowser.includes("'/events?token=' + encodeURIComponent(TOKEN)"), true);
    assert.equal(
      liveBrowser.includes("'/manual-edit-stash?token=' + encodeURIComponent(TOKEN)"),
      true,
    );
    const liveEntrypoint = fs.readFileSync(path.join(scriptsDir, 'live.mjs'), 'utf-8');
    assert.equal(liveEntrypoint.includes('execFileSync(process.execPath'), true);
    assert.equal(liveEntrypoint.includes('execSync('), false);
  } finally {
    if (serverInfo) {
      try {
        runScript(serverScript, ['stop', '--keep-inject'], projectRoot);
      } catch {
        try { process.kill(serverInfo.pid, 'SIGTERM'); } catch {}
      }
    }
    fs.rmSync(fixtureRoot, { recursive: true, force: true });
  }
});
