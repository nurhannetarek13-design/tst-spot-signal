import test from "node:test";
import assert from "node:assert/strict";
import { SignalState } from "../src/index.js";

class AtomicStorage {
  rows = new Map();
  chain = Promise.resolve();
  async get(key) { return structuredClone(this.rows.get(key)); }
  async put(key, value) { this.rows.set(key, structuredClone(value)); }
  transaction(fn) {
    const run = this.chain.then(() => fn({ get: key => this.get(key), put: (key, value) => this.put(key, value) }));
    this.chain = run.catch(() => {});
    return run;
  }
}

test("twenty concurrent BUY callbacks can claim an approval only once", async () => {
  const storage = new AtomicStorage();
  const state = new SignalState({ storage }, {});
  await state.fetch(new Request("https://state.local/put?key=live%3Aapproval%3A12345678", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: { status: "PENDING", symbol: "BTCUSDT" }, expiresAt: Date.now() + 60_000 }),
  }));
  const results = await Promise.all(Array.from({ length: 20 }, (_, i) => state.fetch(new Request("https://state.local/claim?key=live%3Aapproval%3A12345678", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ claimId: `tap-${i}` }),
  })).then(r => r.json())));
  assert.equal(results.filter(x => x.claimed).length, 1);
  assert.equal(results.filter(x => !x.claimed).length, 19);
});
