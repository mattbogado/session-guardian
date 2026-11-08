import { execFileSync } from 'node:child_process';
import { readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { SidecarApp } from 'sidecar_sdk';

const SCRIPT_PATH = fileURLToPath(new URL('./server.py', import.meta.url));
const HTML_URL = new URL('./index.html', import.meta.url);

/**
 * Runs a command mode on `server.py` (`--json`, `--compact`, or `--offload`)
 * and attaches `html_mtime` so the webview hot-reloads when `index.html` changes.
 */
function runServerAction(flag) {
  try {
    const rawJson = execFileSync('python3', [SCRIPT_PATH, flag], {
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
  const initialData = JSON.stringify(runServerAction('--json'));
  return template.replace('__INITIAL_TELEMETRY_JSON__', initialData);
});

app.api('/api/metrics', () => runServerAction('--json'));

app.api('/api/compact', () => runServerAction('--compact'));

app.api('/api/offload', () => runServerAction('--offload'));

app.run();
