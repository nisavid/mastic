#!/usr/bin/env node

import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { cleanWinnerSkipReason, runHook } from './hook-lib.mjs';
import { applyNuxtLiveAdapter, buildNoFollowFlags } from './live-inject.mjs';
import { isPendingEntryActive } from './live/poll-lanes.mjs';

test('clean non-UI acknowledgements have a distinct audit reason', () => {
  assert.equal(cleanWinnerSkipReason(), 'clean-non-ui-ack');
});

test('a non-UI clean file does not suppress a later UI acknowledgement', async () => {
  const projectRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-clean-winner-'));
  const nonUiFile = path.join(projectRoot, 'plain.js');
  const uiFile = path.join(projectRoot, 'component.html');
  fs.writeFileSync(nonUiFile, 'export const value = 1;\n');
  fs.writeFileSync(uiFile, '<main>clean</main>\n');
  try {
    const outcome = await runHook({
      cwd: projectRoot,
      stdinJson: JSON.stringify({
        cwd: projectRoot,
        session_id: 'review-regression',
        tool_name: 'apply_patch',
        tool_input: {
          command: `*** Update File: ${nonUiFile}\n*** Update File: ${uiFile}\n`,
        },
      }),
      detector: {
        detectText: async () => [],
        detectHtml: async () => [],
      },
    });
    assert.equal(outcome.emission?.kind, 'clean');
    assert.equal(outcome.emission?.file, uiFile);
  } finally {
    fs.rmSync(projectRoot, { recursive: true, force: true });
  }
});

test('no-follow writes fail closed when the platform flag is unavailable', () => {
  assert.throws(
    () => buildNoFollowFlags({ O_WRONLY: 1, O_CREAT: 2, O_TRUNC: 4 }),
    /O_NOFOLLOW/,
  );
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
