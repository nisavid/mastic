#!/usr/bin/env node

import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { cleanWinnerSkipReason } from './hook-lib.mjs';
import { applyNuxtLiveAdapter } from './live-inject.mjs';
import { isPendingEntryActive } from './live/poll-lanes.mjs';

test('clean non-UI acknowledgements have a distinct audit reason', () => {
  assert.equal(cleanWinnerSkipReason(), 'clean-non-ui-ack');
});

test('a resumed generation lease must still be pending', () => {
  const entry = { event: { id: 'deadbeef', type: 'generate' } };
  assert.equal(isPendingEntryActive([entry], entry), true);
  assert.equal(isPendingEntryActive([], entry), false);
});

test('Nuxt live adapter rejects a dangling plugin symlink', () => {
  const projectRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-nuxt-symlink-'));
  const pluginsDir = path.join(projectRoot, 'plugins');
  fs.mkdirSync(pluginsDir);
  fs.symlinkSync(
    path.join(projectRoot, '..', 'outside-plugin.ts'),
    path.join(pluginsDir, 'impeccable-live.client.ts'),
  );
  try {
    assert.deepEqual(
      applyNuxtLiveAdapter({
        cwd: projectRoot,
        port: 8400,
        token: 'secret',
        project: { pluginFile: 'plugins/impeccable-live.client.ts' },
      }),
      { file: 'plugins/impeccable-live.client.ts', error: 'nuxt_plugin_symlink' },
    );
    assert.equal(fs.existsSync(path.join(projectRoot, '..', 'outside-plugin.ts')), false);
  } finally {
    fs.rmSync(projectRoot, { recursive: true, force: true });
  }
});
