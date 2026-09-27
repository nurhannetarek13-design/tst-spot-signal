import assert from "node:assert/strict";
import { LIVE_POLICY, evaluateGoNoGo, resolveUnknownExecution, protectionDecision, accountingParity } from "../src/live-cutover-policy.js";

const healthy = {
  supervisorState:"HEALTHY",
  reconciliationOk:true,
  snapshotFresh:true,
  watchdogHealthy:true,
  binanceConnectionOk:true,
  executionRouteHealthy:true,
  unknownOrders:0,
  unprotectedPositions:0,
  dailyLossUSDT:0,
};

let gate=evaluateGoNoGo(healthy);
assert.equal(gate.go,true);
assert.equal(gate.activationAllowed,true);
assert.equal(gate.activationAllowed,false);
assert.deepEqual(gate.activationFailed,["LIVE_EXECUTION_ENABLED","AUTONOMOUS_ENABLED"]);

gate=evaluateGoNoGo({...healthy,policy:{...LIVE_POLICY,liveExecutionEnabled:true,autonomousEnabled:true}});
assert.equal(gate.go,true);

assert.deepEqual(protectionDecision({buyStatus:"REJECTED",executedQty:0,ocoAccepted:false}),{action:"NO_POSITION",protectedQty:0});
assert.deepEqual(protectionDecision({buyStatus:"PARTIALLY_FILLED",executedQty:0.123,ocoAccepted:true}),{action:"PROTECTED",protectedQty:0.123});
const ocoFail=protectionDecision({buyStatus:"FILLED",executedQty:0.123,ocoAccepted:false});
assert.equal(ocoFail.action,"EMERGENCY_CLOSE_REQUIRED");
assert.equal(ocoFail.blockNewTrades,true);

let u=resolveUnknownExecution({transportTimedOut:true,reconciledOrder:null});
assert.equal(u.status,"UNKNOWN_RECONCILIATION_REQUIRED");
assert.equal(u.mayResend,false);
u=resolveUnknownExecution({transportTimedOut:true,reconciledOrder:{status:"FILLED"}});
assert.equal(u.status,"FILLED_FOUND_BY_RECONCILIATION");
assert.equal(u.mayResend,false);
u=resolveUnknownExecution({transportTimedOut:true,reconciledOrder:{status:"PARTIALLY_FILLED"}});
assert.equal(u.status,"ORDER_EXISTS_DO_NOT_RESEND");
assert.equal(u.mayResend,false);

assert.equal(accountingParity({
  bot:{fillQty:1,quoteQty:5.5,commission:0.001,netPnl:-0.01,residualQty:0},
  binance:{fillQty:1,quoteQty:5.5,commission:0.001,netPnl:-0.01,residualQty:0},
}).clean,true);
assert.equal(accountingParity({
  bot:{fillQty:1,quoteQty:5.5,commission:0.001,netPnl:-0.01,residualQty:0.1},
  binance:{fillQty:1,quoteQty:5.5,commission:0.001,netPnl:-0.01,residualQty:0},
}).clean,false);

console.log("LIVE_CUTOVER_FAILURE_PATHS_SELFTEST_PASS");
