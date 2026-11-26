// Antigravity Plus — Control Center & usage sidecar backend.
//
// Serves the Control Center web UI and token-protected APIs:
//   - GET  /               -> index.html
//   - POST /api/recent     -> usage.py --recent 20 (cached 4s)
//   - POST /api/limits     -> LanguageServer RetrieveUserQuotaSummary (cached 10s)
//   - POST /api/session    -> usage.py --conversation <id> --json (cached 1.5s)
//   - POST /api/status     -> status.py --json (cached 2s; POST so the SDK checks the token — it skips GETs)
//   - POST /api/toggle     -> allowlisted memory.py config <key> <on|off> (clears status cache)
//   - POST /api/lsp/stop   -> lsp.py stop (clears status cache)
//   - POST /api/update/check -> update.py check --json (clears status & recent cache)
//
// Security: NEVER modifies bypassMode, protectSecrets, fullAccess, or autoUpdate.
// Run by Antigravity with its bundled Node: "command": "node", "args": ["server.mjs"].

import { execFile } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const USAGE_PY = fileURLToPath(new URL('../../skills/usage/scripts/usage.py', import.meta.url));
const STATUS_PY = fileURLToPath(new URL('./status.py', import.meta.url));
const MEMORY_PY = fileURLToPath(new URL('../../skills/memory/scripts/memory.py', import.meta.url));
const LSP_PY = fileURLToPath(new URL('../../skills/lsp/scripts/lsp.py', import.meta.url));
const UPDATE_PY = fileURLToPath(new URL('../../skills/update/scripts/update.py', import.meta.url));
const PAGE = new URL('./index.html', import.meta.url);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SAFE_LSP_TARGET = /^[A-Za-z0-9_-]{1,64}$/;

// Explicit allowlist of UI-safe coaching & check settings (never bypassMode, protectSecrets, fullAccess, autoUpdate).
export const TOGGLE_CLI_KEYS = Object.freeze({
  autoMemory: 'auto-memory',
  finishTasks: 'finish-tasks',
  checkEdits: 'check-edits',
  autoReview: 'auto-review',
  autoVerify: 'auto-verify',
  autoSetup: 'auto-setup',
  parallelHints: 'parallel-hints',
  speedHints: 'speed-hints',
  progressHints: 'progress-hints',
  quotaHints: 'quota-hints',
  factChecks: 'fact-checks',
  objectiveAnswers: 'objective-answers',
  autoCi: 'auto-ci',
  gitChecks: 'git-checks',
  lspChecks: 'lsp-checks',
  updateCheck: 'update-check',
});

export const SAFE_TOGGLES = new Set(Object.keys(TOGGLE_CLI_KEYS));

// The sidecar inherits Antigravity's PATH; fall back to the usual locations anyway. Windows: the py launcher.
const PYTHONS = process.platform === 'win32'
  ? [['py', ['-3', '-X', 'utf8']], ['python', ['-X', 'utf8']], ['python3', ['-X', 'utf8']]]
  : [['python3', []], ['/usr/bin/python3', []], ['/opt/homebrew/bin/python3', []], ['/usr/local/bin/python3', []]];

class NotFound extends Error {}

export function validateTogglePayload(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return { ok: false, error: 'Request body must be a JSON object with { key, value }' };
  }
  const { key, value } = body;
  if (typeof key !== 'string' || !SAFE_TOGGLES.has(key)) {
    return { ok: false, error: `Setting key is not allowed from Control Center: ${String(key)}` };
  }
  if (typeof value !== 'boolean') {
    return { ok: false, error: `Setting value for ${key} must be a boolean` };
  }
  return { ok: true, key, cliKey: TOGGLE_CLI_KEYS[key], value };
}

export function validateLspTarget(target) {
  if (target === undefined || target === null || target === '') {
    return { ok: true, target: null };
  }
  if (typeof target !== 'string' || !SAFE_LSP_TARGET.test(target)) {
    return { ok: false, error: 'Invalid LSP target identifier' };
  }
  return { ok: true, target };
}

function jsonHttpResponse(ResponseClass, status, payload) {
  const body = JSON.stringify(payload);
  if (typeof ResponseClass === 'function') {
    return new ResponseClass(body, { contentType: 'application/json', status });
  }
  return { status, contentType: 'application/json', body };
}

function runPython(scriptPath, args, { parseJson = true, timeout = 30000 } = {}) {
  return new Promise((resolve, reject) => {
    const attempt = (i) => {
      execFile(
        PYTHONS[i][0],
        [...PYTHONS[i][1], '-I', scriptPath, ...args],
        { timeout, maxBuffer: 64 * 1024 * 1024 },
        (err, stdout, stderr) => {
          if (err && err.code === 'ENOENT' && i + 1 < PYTHONS.length) return attempt(i + 1);
          if (err) {
            const msg = (stderr || err.message || '').trim().slice(0, 400);
            return reject(err.code === 2 ? new NotFound(msg) : new Error(msg || `${scriptPath} failed`));
          }
          if (!parseJson) return resolve(stdout);
          try {
            resolve(JSON.parse(stdout));
          } catch {
            reject(new Error('Python script returned invalid JSON'));
          }
        }
      );
    };
    attempt(0);
  });
}

function resolveLspStopArgs(target) {
  if (!target || target === 'all') return ['stop', '--all'];
  const stateFile = join(homedir(), '.gemini', 'antigravity-plus', 'lsp', `${target}.json`);
  if (!existsSync(stateFile)) return null;
  return ['stop', '--key', target];
}

// Coalesce bursts of identical requests (several panels, fast polling) into one script run.
const memo = new Map();
function cached(key, ttlMs, fn) {
  const hit = memo.get(key);
  if (hit && Date.now() - hit.at < ttlMs) return hit.promise;
  const promise = fn().catch((e) => {
    memo.delete(key);
    throw e;
  });
  memo.set(key, { at: Date.now(), promise });
  if (memo.size > 64) memo.delete(memo.keys().next().value);
  return promise;
}

const recent = () => cached('recent', 4000, () => runPython(USAGE_PY, ['--recent', '20']));
const statusSnapshot = () => cached('status', 2000, () => runPython(STATUS_PY, ['--json']));

// Sidecars get the language server's address ("localhost:<port>", plain HTTP) and their own CSRF token.
function lsBase() {
  const addr = process.env.ANTIGRAVITY_LS_ADDRESS || '';
  return /^https?:\/\//.test(addr) ? addr : `http://${addr}`;
}

async function lsCall(method, body = {}) {
  const res = await fetch(`${lsBase()}/exa.language_server_pb.LanguageServerService/${method}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Connect-Protocol-Version': '1',
      'X-Codeium-Csrf-Token': process.env.ANTIGRAVITY_CSRF_TOKEN || '',
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10000),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || `${method}: HTTP ${res.status}`);
  return data;
}

export function registerRoutes(app, ResponseClass) {
  app.page('/', () => readFileSync(PAGE, 'utf8'));

  app.api('/api/recent', () => recent());

  // 5-hour and weekly limits per model group (Gemini / Claude and GPT), as Antigravity reports them.
  app.api('/api/limits', () => cached('limits', 10000, async () => (await lsCall('RetrieveUserQuotaSummary')).response || {}));

  app.api('/api/session', async ({ conversationId }) => {
    let id = typeof conversationId === 'string' && UUID.test(conversationId) ? conversationId : '';
    if (!id) {
      id = (await recent()).conversations?.[0]?.id || '';
      if (!id) return { empty: true, reason: 'no-conversations' };
    }
    try {
      return await cached(`session:${id}`, 1500, () => runPython(USAGE_PY, ['--conversation', id, '--json']));
    } catch (e) {
      if (e instanceof NotFound) return { empty: true, reason: 'no-transcript', conversation: id };
      throw e;
    }
  });

  // POST, not GET: the sidecar SDK checks X-Sidecar-Token only on non-GET requests, and the snapshot holds the guard
  // log, workspace paths and settings.
  app.api('/api/status', () => statusSnapshot());

  app.api('/api/toggle', async (body) => {
    const check = validateTogglePayload(body);
    if (!check.ok) {
      return jsonHttpResponse(ResponseClass, 400, { error: check.error });
    }
    const updated = await runPython(MEMORY_PY, ['config', check.cliKey, check.value ? 'on' : 'off']);
    memo.delete('status');
    return { ok: true, key: check.key, value: check.value, settings: updated };
  });

  app.api('/api/lsp/stop', async (body) => {
    const check = validateLspTarget(body && typeof body === 'object' ? body.target : undefined);
    if (!check.ok) {
      return jsonHttpResponse(ResponseClass, 400, { error: check.error });
    }
    const args = resolveLspStopArgs(check.target);
    if (!args) {
      memo.delete('status');
      return { ok: true, output: 'Nothing to stop.' };
    }
    // `stop --all` waits up to ~20 s per daemon (at most 3): more than runPython's default 30 s.
    const output = await runPython(LSP_PY, args, { parseJson: false, timeout: 90000 });
    memo.delete('status');
    return { ok: true, output: String(output || '').trim() };
  });

  app.api('/api/update/check', async () => {
    // A clone's check runs `git fetch` (up to 60 s), the GitHub check two 15 s requests.
    const result = await runPython(UPDATE_PY, ['check', '--json'], { timeout: 90000 });
    memo.delete('status');
    memo.delete('recent');
    return result;
  });
}

export async function start() {
  const { SidecarApp, Response } = await import('sidecar_sdk');
  const app = new SidecarApp();
  registerRoutes(app, Response);
  await app.run();
  return app;
}

if (process.env.ANTIGRAVITY_SIDECAR_WEB_PORT) {
  await start();
}
