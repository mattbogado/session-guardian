import { execFileSync } from 'node:child_process';
import { readFileSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SidecarApp } from 'sidecar_sdk';

const SCRIPT_PATH = fileURLToPath(new URL('./server.py', import.meta.url));
const HTML_URL = new URL('./index.html', import.meta.url);
const ANNOTATIONS_DIR = join(homedir(), '.gemini', 'antigravity', 'annotations');

// Stateful focus tracker so when Antigravity stamps `lastUserViewTime = NOW`
// on BOTH the conversation being left (`currentFocusedId`) and the conversation
// being entered (`newId`), we always transition cleanly to `newId`.
const lastSeenViewMs = new Map();
const lastSeenInputMs = new Map();
let currentFocusedId = '';
let currentFocusedTitle = '';

function getAnnotationMtimeNs(convId) {
  try {
    const st = statSync(join(ANNOTATIONS_DIR, `${convId}.pbtxt`), { bigint: true });
    return st.mtimeNs;
  } catch {
    return 0n;
  }
}

/**
 * Queries the Antigravity Language Server (`GetAllCascadeTrajectories`) and detects
 * left-menu conversation transitions via `annotations.lastUserViewTime` deltas.
 */
async function resolveFocusedConversation() {
  const addr = process.env.ANTIGRAVITY_LS_ADDRESS;
  const csrf = process.env.ANTIGRAVITY_CSRF_TOKEN;
  if (!addr || !csrf) {
    return { id: currentFocusedId || 'auto', title: currentFocusedTitle };
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
      return { id: currentFocusedId || 'auto', title: currentFocusedTitle };
    }

    const payload = await res.json();
    const summaries = payload?.trajectorySummaries || {};
    const entries = [];

    for (const [convId, info] of Object.entries(summaries)) {
      if (info?.annotations?.archived) {
        continue;
      }
      const viewMs = Date.parse(info?.annotations?.lastUserViewTime || '') || 0;
      const inputMs = Date.parse(info?.lastUserInputTime || '') || 0;
      const title = info?.annotations?.title || info?.summary || '';
      entries.push({
        id: convId,
        title,
        viewMs,
        inputMs,
        mtimeNs: getAnnotationMtimeNs(convId),
      });
    }

    if (entries.length === 0) {
      return { id: 'auto', title: '' };
    }

    // Initial boot: pick the conversation with the highest viewMs (tie-broken by mtimeNs, then inputMs).
    if (!currentFocusedId || lastSeenViewMs.size === 0) {
      entries.sort((a, b) => {
        if (b.viewMs !== a.viewMs) return b.viewMs - a.viewMs;
        if (b.mtimeNs !== a.mtimeNs) return b.mtimeNs > a.mtimeNs ? 1 : -1;
        return b.inputMs - a.inputMs;
      });
      currentFocusedId = entries[0].id;
      currentFocusedTitle = entries[0].title;
      for (const item of entries) {
        lastSeenViewMs.set(item.id, item.viewMs);
        lastSeenInputMs.set(item.id, item.inputMs);
      }
      return { id: currentFocusedId, title: currentFocusedTitle };
    }

    // Subsequent polls: check which conversations had a new user view or user input event.
    const viewChanged = entries.filter(
      (item) => item.viewMs > (lastSeenViewMs.get(item.id) || 0)
    );
    const inputChanged = entries.filter(
      (item) => item.inputMs > (lastSeenInputMs.get(item.id) || 0)
    );

    for (const item of entries) {
      lastSeenViewMs.set(item.id, item.viewMs);
      lastSeenInputMs.set(item.id, item.inputMs);
    }

    if (inputChanged.length > 0) {
      inputChanged.sort((a, b) => b.inputMs - a.inputMs);
      currentFocusedId = inputChanged[0].id;
      currentFocusedTitle = inputChanged[0].title;
    } else if (viewChanged.length > 0) {
      // When switching from A (`currentFocusedId`) to B in the left sidebar, Antigravity
      // updates `lastUserViewTime` on BOTH A (on blur) and B (on focus) at the same timestamp.
      // Prefer the newly entered conversation (`id !== currentFocusedId`).
      const entered = viewChanged.filter((item) => item.id !== currentFocusedId);
      const pool = entered.length > 0 ? entered : viewChanged;
      pool.sort((a, b) => {
        if (b.viewMs !== a.viewMs) return b.viewMs - a.viewMs;
        if (b.mtimeNs !== a.mtimeNs) return b.mtimeNs > a.mtimeNs ? 1 : -1;
        return b.inputMs - a.inputMs;
      });
      currentFocusedId = pool[0].id;
      currentFocusedTitle = pool[0].title;
    } else {
      // Keep currentFocusedId, refreshing its title if updated.
      const currentEntry = entries.find((item) => item.id === currentFocusedId);
      if (currentEntry && currentEntry.title) {
        currentFocusedTitle = currentEntry.title;
      }
    }

    return { id: currentFocusedId, title: currentFocusedTitle };
  } catch {
    return { id: currentFocusedId || 'auto', title: currentFocusedTitle };
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
