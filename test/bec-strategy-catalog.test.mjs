import test from 'node:test';
import assert from 'node:assert/strict';
import { listBecCandidates } from '../production/bec-strategy-catalog.mjs';

test('BEC candidates are unvalidated by default', () => {
  const xs = listBecCandidates({ timeframe:'1h' });
  assert.ok(xs.length >= 5);
  assert.equal(xs.every(x => x.validated === false), true);
  assert.equal(xs.every(x => x.qualityScore === 0), true);
});

test('only explicit validation records promote a BEC candidate', () => {
  const xs = listBecCandidates({
    timeframe:'1h',
    validation:{
      'bec:dual_momentum_simple': { validated:true, qualityScore:0.82, validationRef:'fixture:test' },
    },
  });
  const promoted = xs.filter(x => x.validated);
  assert.equal(promoted.length, 1);
  assert.equal(promoted[0].id, 'bec:dual_momentum_simple');
  assert.equal(promoted[0].qualityScore, 0.82);
});
