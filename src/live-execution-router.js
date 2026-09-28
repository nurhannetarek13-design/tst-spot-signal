import {
  manualBuyAndProtect as makeManualBuyAndProtect,
  makeReadOnlyHeartbeat,
  MAKE_EXECUTION_ROUTE,
} from "./make-live-client.js";
import {
  manualBuyAndProtectViaSupabase,
  supabaseReadOnlyHeartbeat,
  supabaseReadOnlyReconcile,
  supabaseExecutionConfigured,
} from "./supabase-live-client.js";

export function executionProvider(env = {}) {
  const requested = String(env.EXECUTION_PROVIDER || "MAKE_V2").trim().toUpperCase();
  return requested === "SUPABASE_V2" ? "SUPABASE_V2" : "MAKE_V2";
}

export function executionRoute(env = {}) {
  return executionProvider(env) === "SUPABASE_V2"
    ? "CLOUDFLARE_SIGNED_SUPABASE_BINANCE"
    : "CLOUDFLARE_HMAC_MAKE";
}

export function executorConfigured(env = {}) {
  if (executionProvider(env) === "SUPABASE_V2") return supabaseExecutionConfigured(env);
  return String(env.MAKE_EXECUTOR_V2_READY || "").toLowerCase() === "true";
}

export function executionOwner(env = {}) {
  return executionProvider(env) === "SUPABASE_V2" ? "SUPABASE_EXECUTOR_V2" : "MAKE_EXECUTOR_V2";
}

export function routeVersion(env = {}) {
  return executionProvider(env) === "SUPABASE_V2" ? "supabase-v2" : MAKE_EXECUTION_ROUTE.version;
}

export async function executionReadOnlyReconcile(env) {
  if (executionProvider(env) === "SUPABASE_V2") return supabaseReadOnlyReconcile(env);
  return null;
}

export async function executionReadOnlyHeartbeat(env) {
  if (executionProvider(env) === "SUPABASE_V2") return supabaseReadOnlyHeartbeat(env);
  return makeReadOnlyHeartbeat(env);
}

export async function manualBuyAndProtect(env, input) {
  if (executionProvider(env) === "SUPABASE_V2") {
    return manualBuyAndProtectViaSupabase(env, input);
  }
  return makeManualBuyAndProtect(env, input);
}

export function executionRouteIds(env = {}) {
  if (executionProvider(env) === "SUPABASE_V2") {
    return { buy: "SUPABASE_BUY_V2", oco: "SUPABASE_OCO_V2" };
  }
  return { buy: MAKE_EXECUTION_ROUTE.buy.id, oco: MAKE_EXECUTION_ROUTE.oco.id };
}
