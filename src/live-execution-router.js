import {
  manualBuyAndProtectViaSupabase,
  supabaseReadOnlyHeartbeat,
  supabaseReadOnlyReconcile,
  supabaseExecutionConfigured,
  supabaseDryRunExecution,
  supabaseReconcileActiveTrades,
  supabaseSigningModeProbe,
  supabaseApiKeyOnlyProbe,
  supabaseRelayAuthSelftest,
  supabaseIntentIdempotencySelftest,
  supabasePublicMarketData,
} from "./supabase-live-client.js";

export function executionProvider(env = {}) {
  const requested = String(env.EXECUTION_PROVIDER || "SUPABASE_V2").trim().toUpperCase();
  return requested === "SUPABASE_V2" ? "SUPABASE_V2" : "DISABLED";
}

export function executionRoute(env = {}) {
  return executionProvider(env) === "SUPABASE_V2"
    ? "CLOUDFLARE_SIGNED_SUPABASE_BINANCE"
    : "FINANCIAL_EXECUTION_DISABLED";
}

export function executorConfigured(env = {}) {
  return executionProvider(env) === "SUPABASE_V2" && supabaseExecutionConfigured(env);
}

export function executionOwner(env = {}) {
  return executionProvider(env) === "SUPABASE_V2" ? "SUPABASE_EXECUTOR_V2" : "NONE";
}

export function routeVersion(env = {}) {
  return executionProvider(env) === "SUPABASE_V2" ? "supabase-v2" : "disabled";
}

export async function executionReadOnlyReconcile(env) {
  if (executionProvider(env) === "SUPABASE_V2") return supabaseReadOnlyReconcile(env);
  return { ok:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", financialAction:false };
}

export async function executionReadOnlyHeartbeat(env) {
  if (executionProvider(env) === "SUPABASE_V2") return supabaseReadOnlyHeartbeat(env);
  return { transportOk:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", financialAction:false };
}

export async function manualBuyAndProtect(env, input) {
  if (executionProvider(env) !== "SUPABASE_V2") {
    return { ok:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", noOrderSent:true };
  }
  return manualBuyAndProtectViaSupabase(env, input);
}

export function executionRouteIds(env = {}) {
  if (executionProvider(env) === "SUPABASE_V2") {
    return { buy: "SUPABASE_BUY_V2", oco: "SUPABASE_OCO_V2" };
  }
  return { buy: "DISABLED", oco: "DISABLED" };
}


export async function executionDryRun(env, input) {
  if (executionProvider(env) !== "SUPABASE_V2") {
    return { ok:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", financialAction:false };
  }
  return supabaseDryRunExecution(env, input);
}


export async function executionReconcileActiveTrades(env) {
  if (executionProvider(env) !== "SUPABASE_V2") {
    return { ok:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", financialAction:false };
  }
  return supabaseReconcileActiveTrades(env);
}


export async function executionSigningModeProbe(env) {
  if (executionProvider(env) !== "SUPABASE_V2") {
    return { ok:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", financialAction:false };
  }
  return supabaseSigningModeProbe(env);
}


export async function executionRelayAuthSelftest(env) {
  if (executionProvider(env) !== "SUPABASE_V2") {
    return { ok:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", financialAction:false };
  }
  return supabaseRelayAuthSelftest(env);
}

export async function executionIntentIdempotencySelftest(env) {
  if (executionProvider(env) !== "SUPABASE_V2") {
    return { ok:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", financialAction:false };
  }
  return supabaseIntentIdempotencySelftest(env);
}


export async function executionApiKeyOnlyProbe(env) {
  if (executionProvider(env) !== "SUPABASE_V2") {
    return { ok:false, status:"LEGACY_FINANCIAL_ROUTE_DISABLED", financialAction:false };
  }
  return supabaseApiKeyOnlyProbe(env);
}


export async function executionPublicMarketData(env, path) {
  if (executionProvider(env) !== "SUPABASE_V2") {
    throw new Error("LEGACY_FINANCIAL_ROUTE_DISABLED");
  }
  return supabasePublicMarketData(env, path);
}

// Supabase Vault signer active; Cloudflare forwards authenticated unsigned intents only.

// Supabase runtime state active; Durable Objects are no longer on the critical execution path.
