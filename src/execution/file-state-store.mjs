import fs from 'node:fs/promises';
import path from 'node:path';
import {
  deserializeExecutionState,
  emptyExecutionState,
  normalizeExecutionState,
  serializeExecutionState,
} from './execution-state-store.mjs';

export function createFileStateStore(filePath, { day } = {}) {
  if (!filePath) throw new Error('STATE_FILE_PATH_REQUIRED');
  const resolved = path.resolve(filePath);

  async function load() {
    try {
      const text = await fs.readFile(resolved, 'utf8');
      return deserializeExecutionState(text, day || new Date().toISOString().slice(0, 10));
    } catch (error) {
      if (error?.code === 'ENOENT') return emptyExecutionState(day);
      throw error;
    }
  }

  async function save(next) {
    const normalized = normalizeExecutionState(next, next?.day || day || new Date().toISOString().slice(0, 10));
    await fs.mkdir(path.dirname(resolved), { recursive: true });
    const tmp = `${resolved}.tmp`;
    await fs.writeFile(tmp, `${serializeExecutionState(normalized)}\n`, 'utf8');
    await fs.rename(tmp, resolved);
    return normalized;
  }

  async function reset(resetDay = day || new Date().toISOString().slice(0, 10)) {
    const state = emptyExecutionState(resetDay);
    await save(state);
    return state;
  }

  return { load, save, reset, filePath: resolved };
}
