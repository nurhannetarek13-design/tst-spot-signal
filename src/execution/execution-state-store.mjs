export const EXECUTION_STATE_VERSION = 1;

export function emptyExecutionState(day = new Date().toISOString().slice(0, 10)) {
  return {
    version: EXECUTION_STATE_VERSION,
    day,
    openPositions: 0,
    realizedPnlTodayUsdt: 0,
    positions: {},
    events: [],
  };
}

function validNumber(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

export function normalizeExecutionState(raw, day = new Date().toISOString().slice(0, 10)) {
  const base = emptyExecutionState(day);
  if (!raw || typeof raw !== 'object') return base;

  const sameDay = String(raw.day || '') === day;
  const positions = raw.positions && typeof raw.positions === 'object' ? raw.positions : {};
  return {
    version: EXECUTION_STATE_VERSION,
    day,
    openPositions: Math.max(0, Math.trunc(validNumber(raw.openPositions))),
    realizedPnlTodayUsdt: sameDay ? validNumber(raw.realizedPnlTodayUsdt) : 0,
    positions,
    events: Array.isArray(raw.events) ? raw.events.slice(-200) : [],
  };
}

export function serializeExecutionState(state) {
  return JSON.stringify(normalizeExecutionState(state, String(state?.day || new Date().toISOString().slice(0, 10))));
}

export function deserializeExecutionState(text, day = new Date().toISOString().slice(0, 10)) {
  if (!text) return emptyExecutionState(day);
  try {
    return normalizeExecutionState(JSON.parse(text), day);
  } catch {
    throw new Error('INVALID_EXECUTION_STATE_JSON');
  }
}

export function appendExecutionEvent(state, event) {
  const next = normalizeExecutionState(state, state?.day);
  next.events = [...next.events, { at: new Date().toISOString(), ...event }].slice(-200);
  return next;
}

export function createMemoryStateStore(initialState) {
  let state = normalizeExecutionState(initialState);
  return {
    async load() {
      return structuredClone(state);
    },
    async save(next) {
      state = normalizeExecutionState(next, next?.day || state.day);
      return structuredClone(state);
    },
    async reset(day = new Date().toISOString().slice(0, 10)) {
      state = emptyExecutionState(day);
      return structuredClone(state);
    },
  };
}
