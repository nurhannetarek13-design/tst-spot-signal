import express from "express";
import fs from "node:fs";
import path from "node:path";

const app = express();
app.use(express.json({ limit: "256kb" }));

const POLICY_PATH = process.env.FUSION_POLICY_PATH || path.resolve("fusion/policy.json");
const STATE_FILE = process.env.FUSION_STATE_FILE || "/tmp/tst-fusion-state.json";
const INGEST_TOKEN = process.env.FUSION_INGEST_TOKEN || "";
const policy = JSON.parse(fs.readFileSync(POLICY_PATH, "utf8"));

function emptyState() {
  return {
    validators: { freqtrade: null, jesse: null },
    lastCandidate: null,
    lastDecision: null,
    paper: { openPositions: 0, realizedPnlUSDT: 0, trades: 0, wins: 0 }
  };
}

function readState() {
  try {
    return { ...emptyState(), ...JSON.parse(fs.readFileSync(STATE_FILE, "utf8")) };
  } catch {
    return emptyState();
  }
}

function writeState(state) {
  fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

function ingestAuthorized(req, res, next) {
  if (!INGEST_TOKEN) {
    return res.status(503).json({ ok: false, error: "FUSION_INGEST_TOKEN_REQUIRED" });
  }
  if (req.get("x-fusion-token") !== INGEST_TOKEN) {
    return res.status(401).json({ ok: false, error: "UNAUTHORIZED" });
  }
  next();
}

function ageHours(iso) {
  const t = Date.parse(iso || "");
  return Number.isFinite(t) ? (Date.now() - t) / 3600000 : Infinity;
}

function validatorPass(name, row, strategyId) {
  if (!row) return { ok: false, reason: `${name.toUpperCase()}_MISSING` };
  if (row.strategyId !== strategyId) return { ok: false, reason: `${name.toUpperCase()}_STRATEGY_MISMATCH` };
  if (ageHours(row.updatedAt) > policy.decisionGate.validatorMaxAgeHours) {
    return { ok: false, reason: `${name.toUpperCase()}_STALE` };
  }
  const m = row.metrics || {};
  if (row.pass !== true) return { ok: false, reason: `${name.toUpperCase()}_FAILED` };
  if (Number(m.trades || 0) < policy.decisionGate.minTrades) return { ok: false, reason: `${name.toUpperCase()}_TOO_FEW_TRADES` };
  if (Number(m.profitFactor || 0) < policy.decisionGate.minProfitFactor) return { ok: false, reason: `${name.toUpperCase()}_LOW_PF` };
  if (!(Number(m.expectancy || 0) > 0)) return { ok: false, reason: `${name.toUpperCase()}_NEG_EXPECTANCY` };
  if (m.stressPass !== true) return { ok: false, reason: `${name.toUpperCase()}_STRESS_FAILED` };
  return { ok: true };
}

function normalizedSymbol(symbol) {
  return String(symbol || "").toUpperCase().replace("/", "-").replace("USDT", "USDT");
}

function decide(candidate, state) {
  const reasons = [];
  const symbol = normalizedSymbol(candidate.symbol);
  const strategyId = candidate.strategyId || policy.strategyId;

  if (!["LONG", "BUY"].includes(String(candidate.side || "").toUpperCase())) reasons.push("LONG_ONLY");
  if (!symbol.endsWith("USDT")) reasons.push("USDT_SPOT_ONLY");
  if (Number(candidate.score || 0) < policy.decisionGate.minCandidateScore) reasons.push("SCORE_TOO_LOW");
  if (!(Number(candidate.notionalUSDT || 0) > 0) || Number(candidate.notionalUSDT) > policy.account.maxPositionUSDT) reasons.push("POSITION_LIMIT");
  if (!(Number(candidate.riskUSDT || 0) > 0) || Number(candidate.riskUSDT) > policy.account.maxRiskUSDT) reasons.push("RISK_LIMIT");
  if (Number(state.paper?.openPositions || 0) >= policy.account.maxOpenPositions) reasons.push("MAX_OPEN_POSITIONS");
  if (Number(state.paper?.realizedPnlUSDT || 0) <= -policy.account.dailyLossCapUSDT) reasons.push("DAILY_LOSS_CAP");

  for (const name of policy.decisionGate.validatorsRequired) {
    const check = validatorPass(name, state.validators?.[name], strategyId);
    if (!check.ok) reasons.push(check.reason);
  }

  const paperApproved = reasons.length === 0;
  return {
    ok: true,
    release: policy.release,
    strategyId,
    decision: paperApproved ? "PAPER_APPROVED" : "NO_TRADE",
    executorAllowed: false,
    liveTrading: false,
    reasons,
    checkedAt: new Date().toISOString()
  };
}

app.get("/health", (req, res) => {
  res.json({ ok: true, service: "tst-fusion-master", mode: policy.mode, liveTrading: false });
});

app.get("/status", (req, res) => {
  const state = readState();
  res.json({
    ok: true,
    policy: {
      release: policy.release,
      mode: policy.mode,
      strategyId: policy.strategyId,
      execution: policy.execution
    },
    state
  });
});

app.post("/validators/:engine", ingestAuthorized, (req, res) => {
  const engine = String(req.params.engine || "").toLowerCase();
  if (!["freqtrade", "jesse"].includes(engine)) {
    return res.status(400).json({ ok: false, error: "UNKNOWN_VALIDATOR" });
  }

  const metrics = req.body?.metrics || {};
  const row = {
    engine,
    strategyId: req.body?.strategyId || policy.strategyId,
    pass: req.body?.pass === true,
    metrics: {
      trades: Number(metrics.trades || 0),
      profitFactor: Number(metrics.profitFactor || 0),
      expectancy: Number(metrics.expectancy || 0),
      maxDrawdown: Number(metrics.maxDrawdown || 0),
      stressPass: metrics.stressPass === true
    },
    source: req.body?.source || null,
    updatedAt: new Date().toISOString()
  };

  const state = readState();
  state.validators[engine] = row;
  writeState(state);
  res.json({ ok: true, validator: row });
});

app.post("/candidate/hummingbot", ingestAuthorized, (req, res) => {
  const candidate = {
    strategyId: req.body?.strategyId || policy.strategyId,
    symbol: req.body?.symbol,
    side: req.body?.side,
    score: Number(req.body?.score || 0),
    regime: req.body?.regime || null,
    entry: Number(req.body?.entry || 0),
    stop: Number(req.body?.stop || 0),
    target: Number(req.body?.target || 0),
    notionalUSDT: Number(req.body?.notionalUSDT || 0),
    riskUSDT: Number(req.body?.riskUSDT || 0),
    receivedAt: new Date().toISOString()
  };

  const state = readState();
  const decision = decide(candidate, state);
  state.lastCandidate = candidate;
  state.lastDecision = decision;
  writeState(state);
  res.json(decision);
});

app.post("/paper/close", ingestAuthorized, (req, res) => {
  const pnl = Number(req.body?.pnlUSDT || 0);
  const state = readState();
  state.paper.realizedPnlUSDT = Number(state.paper.realizedPnlUSDT || 0) + pnl;
  state.paper.trades = Number(state.paper.trades || 0) + 1;
  state.paper.wins = Number(state.paper.wins || 0) + (pnl > 0 ? 1 : 0);
  state.paper.openPositions = Math.max(0, Number(state.paper.openPositions || 0) - 1);
  writeState(state);
  res.json({ ok: true, paper: state.paper });
});

const port = Number(process.env.PORT || 8787);
const host = process.env.FUSION_BIND || "0.0.0.0";
app.listen(port, host, () => {
  console.log(`TST Fusion Master listening on ${host}:${port} in ${policy.mode}`);
});
