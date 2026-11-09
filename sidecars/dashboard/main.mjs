import { execFileSync } from 'node:child_process';
import { readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { SidecarApp } from 'sidecar_sdk';

const SCRIPT_PATH = fileURLToPath(new URL('./server.py', import.meta.url));
const HTML_URL = new URL('./index.html', import.meta.url);

/**
 * Queries the Antigravity Language Server (`GetAllCascadeTrajectories`) to find
 * the exact conversation currently selected by the user in the left sidebar menu
 * via `annotations.lastUserViewTime` and `lastUserInputTime`.
 */
async function resolveFocusedConversation() {
  const addr = process.env.ANTIGRAVITY_LS_ADDRESS;
  const csrf = process.env.ANTIGRAVITY_CSRF_TOKEN;
  if (!addr || !csrf) {
    return { id: 'auto', title: '' };
  }
  try {
    const res = await fetch(
      `http://${addr}/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-codeium-csrf-token': csrf,
        },
        body: '{}',
      }
    );
    if (!res.ok) {
      return { id: 'auto', title: '' };
    }
    const payload = await res.json();
    const summaries = payload?.trajectorySummaries || {};
    let bestId = 'auto';
    let bestTitle = '';
    let bestViewMs = -1;
    let bestInputMs = -1;

    for (const [convId, info] of Object.entries(summaries)) {
      if (info?.annotations?.archived) {
        continue;
      }
      const viewMs = Date.parse(info?.annotations?.lastUserViewTime || '') || 0;
      const inputMs = Date.parse(info?.lastUserInputTime || '') || 0;
      if (
        viewMs > bestViewMs ||
        (viewMs === bestViewMs && inputMs > bestInputMs)
      ) {
        bestViewMs = viewMs;
        bestInputMs = inputMs;
        bestId = convId;
        bestTitle = info?.annotations?.title || info?.summary || '';
      }
    }
    return { id: bestId, title: bestTitle };
  } catch {
    return { id: 'auto', title: '' };
  }
}

/**
 * Runs a command mode on `server.py` (`--json`, `--compact`, or `--offload`)
 * for the currently focused conversation in the left menu.
 */
async function runServerAction(flag) {
  try {
    const focused = await resolveFocusedConversation();
    const args = [SCRIPT_PATH, flag];
    if (focused.id && focused.id !== 'auto') {
      args.push('--conversation-id', String(focused.id));
    }
    const rawJson = execFileSync('python3', args, {
      encoding: 'utf8',
      timeout: 8000,
    });
    const parsed = JSON.parse(rawJson);
    if (focused.title) {
      parsed.conversation_title = focused.title;
    }
    parsed.html_mtime = statSync(HTML_URL).mtimeMs;
    return parsed;
  } catch (error) {
    return {
      ok: false,
      found: false,
      message: `Action ${flag} failed: ${error.message}`,
    };
  }
}

const app = new SidecarApp();

app.page('/', async () => {
  const template = readFileSync(HTML_URL, 'utf8');
  const initialData = JSON.stringify(await runServerAction('--json'));
  return template.replace('__INITIAL_TELEMETRY_JSON__', initialData);
});

app.api('/api/metrics', () => runServerAction('--json'), 'POST');
app.api('/api/metrics', () => runServerAction('--json'), 'GET');

app.api('/api/compact', () => runServerAction('--compact'), 'POST');

app.api('/api/offload', () => runServerAction('--offload'), 'POST');

app.run();
