import { execFileSync } from 'node:child_process';
import { readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { SidecarApp } from 'sidecar_sdk';

const SCRIPT_PATH = fileURLToPath(new URL('./server.py', import.meta.url));
const HTML_URL = new URL('./index.html', import.meta.url);

/**
 * Runs a command mode on `server.py` (`--json`, `--compact`, or `--offload`)
 * with an optional target conversation ID (`--conversation-id <id>`), and
 * attaches `html_mtime` so the webview hot-reloads when `index.html` changes.
 */
function runServerAction(flag, conversationId = 'auto') {
  try {
    const args = [SCRIPT_PATH, flag];
    if (conversationId && conversationId !== 'auto') {
      args.push('--conversation-id', String(conversationId));
    }
    const rawJson = execFileSync('python3', args, {
      encoding: 'utf8',
      timeout: 8000,
    });
    const parsed = JSON.parse(rawJson);
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

app.page('/', () => {
  const template = readFileSync(HTML_URL, 'utf8');
  const initialData = JSON.stringify(runServerAction('--json', 'auto'));
  return template.replace('__INITIAL_TELEMETRY_JSON__', initialData);
});

app.api('/api/metrics', (data) => runServerAction('--json', data?.conversationId), 'POST');
app.api('/api/metrics', (data) => runServerAction('--json', data?.conversationId), 'GET');

app.api('/api/compact', (data) => runServerAction('--compact', data?.conversationId), 'POST');

app.api('/api/offload', (data) => runServerAction('--offload', data?.conversationId), 'POST');

app.run();
