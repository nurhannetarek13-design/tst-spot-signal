// JSON-lines-free, single-shot bridge. Imports ORIGINAL strategy modules; no rewritten triggers.
// Never submits orders or reads private credentials.
import { readFileSync } from 'node:fs';
import { scoreCandidate as momentum, STRATEGY_ID as momentumId } from '../src/strategies/regime-adaptive-momentum.mjs';
import { scoreCandidate as smallCap, STRATEGY_ID as smallId } from '../src/strategies/small-cap-intraday.mjs';

function main() {
  const payload = JSON.parse(readFileSync(0, 'utf8'));
  if (!payload || !['momentum', 'small_cap'].includes(payload.source)) throw Error('UNKNOWN_SOURCE');
  const result = payload.source === 'momentum'
    ? momentum(payload.input)
    : smallCap(payload.input);
  const expected = payload.source === 'momentum' ? momentumId : smallId;
  if (result?.strategy !== expected || result?.liveApproved !== false) throw Error('INVALID_SOURCE_RESULT');
  process.stdout.write(JSON.stringify(result) + '\n');
}

try { main(); } catch (e) {
  process.stderr.write(String(e?.message || e) + '\n');
  process.exitCode = 1;
}
