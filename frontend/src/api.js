// ── Backend API helpers ────────────────────────────────────────────────────────

export function backendBase(state) {
  return state.backendUrl || 'http://localhost:8080';
}

export function backendHeaders(state) {
  const h = { 'Content-Type': 'application/json' };
  if (state.backendKey) h['X-API-Key'] = state.backendKey;
  return h;
}

export async function backendPost(state, path, body) {
  const r = await fetch(backendBase(state) + path, {
    method: 'POST',
    headers: backendHeaders(state),
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(30000),
  });
  if (!r.ok) {
    const t = await r.text().catch(() => '');
    let msg = t;
    try { msg = JSON.parse(t).detail || t; } catch {}
    throw new Error(`${r.status}: ${msg.slice(0, 200)}`);
  }
  return r.json();
}

export async function backendGet(state, path) {
  const h = {};
  if (state.backendKey) h['X-API-Key'] = state.backendKey;
  const r = await fetch(backendBase(state) + path, {
    headers: h,
    signal: AbortSignal.timeout(30000),
  });
  if (!r.ok) {
    const t = await r.text().catch(() => '');
    let msg = t;
    try { msg = JSON.parse(t).detail || t; } catch {}
    throw new Error(`${r.status}: ${msg.slice(0, 200)}`);
  }
  return r.json();
}

export function gitCfg(state) {
  return {
    provider: state.provider,
    base_url: state.baseUrl || '',
    auth_mode: state.authMode,
    token: state.token || '',
    username: state.username || '',
    password: state.password || '',
    workspace: state.workspace || '',
    project_key: state.projectKey || '',
  };
}

export async function gitProxyPost(state, path, body) {
  if (!state.backendUrl) throw new Error('Backend URL not configured. Go to Settings.');
  return backendPost(state, path, body);
}

export async function fetchCiaaRole(state) {
  if (!state.backendUrl || !state.backendKey) return null;
  try {
    const d = await backendGet(state, '/api/v1/me');
    return d;
  } catch(_) {
    return null;
  }
}

export async function downloadCsv(state, path) {
  const h = {};
  if (state.backendKey) h['X-API-Key'] = state.backendKey;
  const r = await fetch(`${state.backendUrl}${path}`, { headers: h });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const fn = (r.headers.get('content-disposition') || '').match(/filename="([^"]+)"/);
  a.href = url;
  a.download = fn ? fn[1] : 'gto_export.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
