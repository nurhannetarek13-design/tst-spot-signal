import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs/promises';
import { createFileStateStore } from '../src/execution/file-state-store.mjs';
import { createTelegramNotifier, formatTelegramExecutionEvent } from '../src/execution/telegram-notifier.mjs';

test('file state store persists normalized execution state', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'tst-state-'));
  const file = path.join(dir, 'state.json');
  const store = createFileStateStore(file, { day: '2026-09-06' });
  await store.save({ day: '2026-09-06', openPositions: 1, realizedPnlTodayUsdt: -0.25, positions: { ABCUSDT: { mode: 'PAPER' } }, events: [] });
  const loaded = await store.load();
  assert.equal(loaded.openPositions, 1);
  assert.equal(loaded.realizedPnlTodayUsdt, -0.25);
  assert.ok(loaded.positions.ABCUSDT);
});

test('telegram notifier is fail-soft when not configured', async () => {
  const notifier = createTelegramNotifier();
  const out = await notifier.notify({ type: 'PAPER_ENTRY_FILLED', symbol: 'ABCUSDT' });
  assert.deepEqual(out, { sent: false, reason: 'TELEGRAM_NOT_CONFIGURED' });
});

test('telegram execution event contains protection levels', () => {
  const text = formatTelegramExecutionEvent({ type: 'PROTECTION_PLACED', symbol: 'ABCUSDT', stopPrice: 1.9, takeProfitPrice: 2.2 });
  assert.match(text, /ABCUSDT/);
  assert.match(text, /Stop: 1.9/);
  assert.match(text, /Target: 2.2/);
});
