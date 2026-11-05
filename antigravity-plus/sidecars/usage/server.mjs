// Antigravity Plus — usage side panel (sidecar).
//
// Serves the panel page and token-protected APIs. The numbers come from the /usage script
// (skills/usage/scripts/usage.py --json / --recent), so the panel and /usage always agree;
// plan limits (/api/limits) come straight from Antigravity's language server.
// Run by Antigravity with its bundled Node: "command": "node", "args": ["server.mjs"].

import { SidecarApp } from 'sidecar_sdk';
import { execFile } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const USAGE_PY = fileURLToPath(new URL('../../skills/usage/scripts/usage.py', import.meta.url));
const PAGE = new URL('./index.html', import.meta.url);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// The sidecar inherits Antigravity's PATH; fall back to the usual locations anyway. Windows: the py launcher.
const PYTHONS = process.platform === 'win32'
  ? [['py', ['-3', '-X', 'utf8']], ['python', ['-X', 'utf8']], ['python3', ['-X', 'utf8']]]
  : [['python3', []], ['/usr/bin/python3', []], ['/opt/homebrew/bin/python3', []], ['/usr/local/bin/python3', []]];

class NotFound extends Error {}

function runUsage(args) {
  return new Promise((resolve, reject) => {
    const attempt = (i) => {
      execFile(PYTHONS[i][0], [...PYTHONS[i][1], '-I', USAGE_PY, ...args], { timeout: 30000, maxBuffer: 64 * 1024 * 1024 },
        (err, stdout, stderr) => {
          if (err && err.code === 'ENOENT' && i + 1 < PYTHONS.length) return attempt(i + 1);
          if (err) {
            const msg = (stderr || err.message || '').trim().slice(0, 400);
            return reject(err.code === 2 ? new NotFound(msg) : new Error(msg || 'usage.py failed'));
          }
          try {
            resolve(JSON.parse(stdout));
          } catch {
            reject(new Error('usage.py returned invalid JSON'));
          }
        });
    };
    attempt(0);
  });
}

// Coalesce bursts of identical requests (several panels, fast polling) into one usage.py run.
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

const recent = () => cached('recent', 4000, () => runUsage(['--recent', '20']));

// Sidecars get the language server's address ("localhost:<port>", plain HTTP) and their own CSRF token.
const LS_ADDR = process.env.ANTIGRAVITY_LS_ADDRESS || '';
const LS_BASE = /^https?:\/\//.test(LS_ADDR) ? LS_ADDR : `http://${LS_ADDR}`;

async function lsCall(method, body = {}) {
  const res = await fetch(`${LS_BASE}/exa.language_server_pb.LanguageServerService/${method}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Connect-Protocol-Version': '1',
               'X-Codeium-Csrf-Token': process.env.ANTIGRAVITY_CSRF_TOKEN || '' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10000),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || `${method}: HTTP ${res.status}`);
  return data;
}

const app = new SidecarApp();

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
    return await cached(`session:${id}`, 1500, () => runUsage(['--conversation', id, '--json']));
  } catch (e) {
    if (e instanceof NotFound) return { empty: true, reason: 'no-transcript', conversation: id };
    throw e;
  }
});

await app.run();
