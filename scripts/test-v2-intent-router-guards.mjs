import assert from "node:assert/strict";
import { classifyDuplicateIntent } from "../src/supabase-live-client.js";
import { executionProvider, executionRoute, executorConfigured, executionRouteIds } from "../src/live-execution-router.js";

for (const state of ["SIGNAL_CREATED","APPROVED"]) {
  const r=classifyDuplicateIntent({state});
  assert.equal(r.status,"DUPLICATE_TRADE_INTENT_BLOCKED");
  assert.equal(r.reconciliationRequired,false);
}
for (const state of ["ENTRY_SUBMITTING","ENTRY_ACCEPTED","PARTIALLY_FILLED","FILLED","PROTECTION_PENDING","PROTECTED"]) {
  const r=classifyDuplicateIntent({state});
  assert.equal(r.status,"DUPLICATE_TRADE_INTENT_BLOCKED");
  assert.equal(r.reconciliationRequired,true);
}
assert.deepEqual(classifyDuplicateIntent({state:"EXITED"}),{
  status:"TRADE_INTENT_ALREADY_EXITED",reconciliationRequired:false,
});
assert.deepEqual(classifyDuplicateIntent({state:"FAILED_SAFE"}),{
  status:"TRADE_INTENT_FAILED_SAFE_LOCKED",reconciliationRequired:false,
});

const disabled={EXECUTION_PROVIDER:"MAKE_V2",MAKE_EXECUTOR_V2_READY:"true"};
assert.equal(executionProvider(disabled),"DISABLED");
assert.equal(executionRoute(disabled),"FINANCIAL_EXECUTION_DISABLED");
assert.equal(executorConfigured(disabled),false);
assert.deepEqual(executionRouteIds(disabled),{buy:"DISABLED",oco:"DISABLED"});

const typo={EXECUTION_PROVIDER:"SUPABASE_V3",MAKE_EXECUTOR_V2_READY:"true"};
assert.equal(executionProvider(typo),"DISABLED");
assert.equal(executionRoute(typo),"FINANCIAL_EXECUTION_DISABLED");

console.log("V2_INTENT_AND_ROUTER_GUARDS_PASS");
