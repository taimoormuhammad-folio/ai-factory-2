const BASE = import.meta.env.VITE_API_URL || '/api';

export class ApiError extends Error {
  constructor(message, { status, runId, code } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.runId = runId;
    this.code = code;
  }
}

async function fetchJson(path, init = {}) {
  const defaultTimeoutMs =
    init.method === 'POST' && path === '/runs'
      ? 60000
      : path === '/runs' && (!init.method || init.method === 'GET')
        ? 60000
        : 15000;
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    signal: init.signal ?? AbortSignal.timeout(defaultTimeoutMs),
  });
  if (!res.ok) {
    const raw = await res.text();
    let message = raw || res.statusText;
    let runId;
    try {
      const parsed = JSON.parse(raw);
      const detail = parsed.detail;
      if (typeof detail === 'object' && detail !== null) {
        message = detail.message || message;
        runId = detail.run_id;
      } else if (typeof detail === 'string') {
        message = detail;
      }
    } catch (_) {
      /* plain text error body */
    }
    if (res.status === 409 && runId) {
      throw new ApiError(message, { status: 409, runId, code: 'RUN_IN_PROGRESS' });
    }
    if (res.status === 409 && path.includes('/resume')) {
      try {
        const parsed = JSON.parse(raw);
        const detail = parsed.detail;
        if (typeof detail === 'object' && detail?.run_id) {
          throw new ApiError(detail.message || message, {
            status: 409,
            runId: detail.run_id,
            code: 'RUN_IN_PROGRESS',
          });
        }
      } catch (err) {
        if (err instanceof ApiError) throw err;
      }
    }
    throw new ApiError(message, { status: res.status });
  }
  return res.json();
}

export function listRuns() {
  return fetchJson('/runs');
}

export function getRun(runId) {
  return fetchJson(`/runs/${runId}`);
}

export function getCurrentRun() {
  return fetchJson('/runs/current');
}

export function fetchRandomBrief() {
  return fetchJson('/briefs/random');
}

export function startRun(body) {
  return fetchJson('/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function resumeRun(runId) {
  return fetchJson(`/runs/${runId}/resume`, {
    method: 'POST',
  });
}

/** Stop QA loops and mark run complete when all work items are built (manual Lumen demo). */
export function markRunDemoReady(runId) {
  return fetchJson(`/runs/${runId}/demo-ready`, { method: 'POST' });
}

/** Approve or reject an FZ async gate (prd / architecture / ui / …). */
export function decideGate(runId, gate, {
  approved,
  feedback = '',
  approver = 'Console User',
  role = '',
  risk_note = '',
  resume = true,
} = {}) {
  return fetchJson(`/runs/${runId}/gates/${gate}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved, feedback, approver, role, risk_note, resume }),
  });
}

export function startDeepEval(runId) {
  return fetchJson(`/runs/${runId}/deepeval`, { method: 'POST' });
}

export function getDeepEvalStatus(runId) {
  return fetchJson(`/runs/${runId}/deepeval`);
}

export function deepEvalReportUrl(runId) {
  return `${BASE}/runs/${runId}/deepeval.html`;
}

export function fetchArtifact(path, runId) {
  const url = runId
    ? `/runs/${runId}/artifacts/${path}`
    : `/artifacts/${path}`;
  return fetchJson(url);
}

export function fetchAuditEvents(runId) {
  return fetchJson(`/runs/${runId}/audit`);
}

/** Download merged SDLC dossier PDF for multiple runs (comma-separated ids on server). */
export async function downloadCombinedDossier(runIds) {
  const runs = (runIds || []).filter(Boolean).join(',');
  if (!runs) throw new Error('No run ids provided');
  const res = await fetch(`${BASE}/dossier/combined?runs=${encodeURIComponent(runs)}&format=pdf`, {
    credentials: 'include',
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Combined dossier download failed (${res.status})`);
  }
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `ai-factory-dossier-combined-${runIds[0]}.pdf`;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function downloadDossierBlob(runId, format) {
  const fmt = format === 'html' ? 'html' : 'pdf';
  const res = await fetch(`${BASE}/runs/${encodeURIComponent(runId)}/dossier?format=${fmt}`, {
    signal: AbortSignal.timeout(120000),
  });
  if (!res.ok) {
    const raw = await res.text();
    let message = raw || res.statusText;
    try {
      const parsed = JSON.parse(raw);
      if (typeof parsed.detail === 'string') message = parsed.detail;
    } catch (_) {
      /* plain text */
    }
    throw new ApiError(message, { status: res.status });
  }
  const blob = await res.blob();
  const ext = fmt === 'html' ? 'html' : 'pdf';
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objectUrl;
  a.download = `ai-factory-dossier-${runId}.${ext}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(objectUrl);
}

/** Download SDLC dossier (read-only; safe during active runs). format: pdf | html */
export async function downloadRunDossier(runId, format = 'pdf') {
  await downloadDossierBlob(runId, format);
}

export function previewUrl(runId) {
  return `${BASE}/runs/${runId}/preview/index.html`;
}

export function listEmulatorDevices(runId) {
  return fetchJson(`/runs/${runId}/emulator/devices`);
}

export function getEmulatorStatus(runId) {
  return fetchJson(`/runs/${runId}/emulator/status`);
}

export function startEmulatorRun(runId, deviceId) {
  return fetchJson(`/runs/${runId}/emulator/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(deviceId ? { device_id: deviceId } : {}),
  });
}

export function stopEmulatorRun(runId) {
  return fetchJson(`/runs/${runId}/emulator/stop`, { method: 'POST' });
}

export function subscribeEvents(runId, onState, onAudit) {
  const url = runId
    ? `${BASE}/runs/${runId}/events`
    : `${BASE}/runs/current/events`;

  const source = new EventSource(url);

  source.addEventListener('state', (e) => {
    onState(JSON.parse(e.data));
  });

  source.addEventListener('audit', (e) => {
    onAudit(JSON.parse(e.data));
  });

  return () => source.close();
}
