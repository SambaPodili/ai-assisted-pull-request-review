// ── Constants ─────────────────────────────────────────────────────────────────

export const AGENT_META = {
  code_analysis:   { label:'Code Analysis',       icon:'ti-code',              phase:'Phase 1',  color:'#1a6cf6', desc:'Classifies the change (feature / refactor / bugfix / config) and flags code-quality issues and complexity changes.' },
  security:        { label:'Security Review',      icon:'ti-shield-lock',       phase:'Phase 1',  color:'#ef4444', desc:'Scans for vulnerabilities (injection, auth, crypto, CWE Top-25). Tuned for low false positives; can BLOCK the gate.' },
  ast_analysis:    { label:'AST Analysis',         icon:'ti-binary-tree',       phase:'Phase 1b', color:'#8b5cf6', desc:'Parses changed code into a syntax tree to reason about structure and complexity — not just raw text.' },
  secrets_entropy: { label:'Entropy / Secrets',    icon:'ti-key',               phase:'Phase 1b', color:'#ec4899', desc:'Detects hardcoded secrets via Shannon entropy + known key prefixes; skips ordinary code identifiers. Secrets BLOCK the gate.' },
  taint_analysis:  { label:'Taint Analysis',       icon:'ti-arrows-diff',       phase:'Phase 1b', color:'#f97316', desc:'Tracks user-controlled data from source to sink to prove injection / SSRF / path-traversal. BLOCKs the gate.' },
  iac_analysis:    { label:'IaC Scan',             icon:'ti-server',            phase:'Phase 1b', color:'#14b8a6', desc:'Terraform / Kubernetes / Docker misconfigurations (public buckets, wildcard IAM, privileged containers). Critical → BLOCK.' },
  temporal_risk:   { label:'Temporal Risk',        icon:'ti-clock-record',      phase:'Phase 1b', color:'#a855f7', desc:'Looks across PR history for risky patterns — repeatedly-touched hot files and escalating change risk.' },
  schema_change:      { label:'Schema Changes',       icon:'ti-database',          phase:'Phase 1b', color:'#06b6d4', desc:'Detects DB / migration risk (Flyway, Liquibase, .sql). Destructive + irreversible migrations BLOCK the gate.' },
  qa_scenarios:       { label:'QA Scenarios',          icon:'ti-checklist',         phase:'Phase 1b', color:'#d97706', desc:'Generates test scenarios with preconditions, acceptance criteria and runnable skeletons; requirement-aware from uploaded docs.' },
  reference_impact:   { label:'Reference Impact',      icon:'ti-git-branch',        phase:'Phase 1b', color:'#7c3aed', desc:'Finds where changed symbols are used across the codebase — the call graph and blast radius of the change.' },
  performance_impact: { label:'Performance Impact',    icon:'ti-rocket',            phase:'Phase 1b', color:'#0284c7', desc:'Flags N+1 queries, complexity regressions and hot-path risks introduced by the change.' },
  data_privacy:       { label:'Data Privacy',          icon:'ti-lock',              phase:'Phase 1b', color:'#db2777', desc:'Detects unencrypted PII handling (GDPR / PCI-DSS / PDPA). Unencrypted PII holds the gate.' },
  maintainability:    { label:'Maintainability',       icon:'ti-tool',              phase:'Phase 1b', color:'#6366f1', desc:'Flags long functions, duplication and complexity smells that hurt long-term maintainability.' },
  license_compliance: { label:'License Compliance',    icon:'ti-license',           phase:'Phase 1b', color:'#059669', desc:'Checks newly-added dependencies for copyleft / unknown licences. Copyleft introduction BLOCKs the gate.' },
  observability:      { label:'Observability',         icon:'ti-eye',               phase:'Phase 1b', color:'#0ea5e9', desc:'Detects removed logs / metrics and missing instrumentation on new code paths.' },
  functional_validation: { label:'FSD Validation',       icon:'ti-file-check',        phase:'Phase 1b', color:'#6366f1', desc:'Validates the change against uploaded Functional Specification Documents and reports business-function impact across dependent repos.' },
  cross_repo_impact:  { label:'Cross-Repo Impact',     icon:'ti-affiliate',         phase:'Phase 1b', color:'#e11d48', desc:'Deep analysis of declared downstream repos: for each call-site of a changed symbol, judges whether the change breaks that caller (signature/removal) and the exact fix.' },
  dependency:         { label:'Dependency Mapping',    icon:'ti-topology-star-3',   phase:'Phase 2',  color:'#f59e0b', desc:'Computes blast radius across the service dependency graph and checks changed dependencies for known CVEs (OSV). CVEs BLOCK.' },
  test_coverage:      { label:'Test Coverage',         icon:'ti-test-pipe',         phase:'Phase 2',  color:'#10b981', desc:'Finds test gaps, validates per-method scenario coverage, flags assertion-free (hollow) tests, and generates stubs.' },
  interface:          { label:'Interface / API',       icon:'ti-api',               phase:'Phase 2',  color:'#3b82f6', desc:'Detects contract-breaking changes in REST / gRPC / AsyncAPI / MQ and traces downstream consumer impact. Breaking → HOLD.' },
  risk:               { label:'Risk Assessment',       icon:'ti-scale',             phase:'Phase 2',  color:'#0ea5e9', desc:'Synthesises every finding into a composite risk score and a proposed gate decision (the policy then enforces the final gate).' },
  remediation:        { label:'Remediation',           icon:'ti-tool',              phase:'Phase 3',  color:'#84cc16', desc:'Produces concrete fix diffs, a deployment strategy, and the executive summary.' },
};

export const AGENT_ORDER = [
  'code_analysis','security',
  'ast_analysis','secrets_entropy','taint_analysis','iac_analysis','temporal_risk',
  'schema_change','qa_scenarios','reference_impact',
  'performance_impact','data_privacy','maintainability','license_compliance','observability','functional_validation','cross_repo_impact',
  'dependency','test_coverage','interface','risk','remediation',
];

export const MODEL_PROVIDERS = {
  anthropic:    {
    label:'Anthropic Claude', icon:'✦',
    models:['claude-sonnet-4-6','claude-haiku-4-5-20251001','claude-opus-4-6'],
    needsKey:true, needsUrl:false,
    keyPlaceholder:'sk-ant-api03-…', urlPlaceholder:'',
    hint:'Best quality for code analysis. <a href="https://console.anthropic.com/settings/keys" target="_blank">Get key ↗</a>',
  },
  openai:       {
    label:'OpenAI', icon:'⬡',
    models:['gpt-4o','gpt-4o-mini','gpt-4-turbo','gpt-3.5-turbo'],
    needsKey:true, needsUrl:false,
    keyPlaceholder:'sk-…', urlPlaceholder:'',
    hint:'Strong alternative. <a href="https://platform.openai.com/api-keys" target="_blank">Get key ↗</a>',
  },
  azure_openai: {
    label:'Azure OpenAI', icon:'⬡',
    models:['gpt-4o','gpt-4-turbo','gpt-35-turbo'],
    needsKey:true, needsUrl:true,
    keyPlaceholder:'Azure API key', urlPlaceholder:'https://YOUR.openai.azure.com',
    hint:'Your Azure-hosted OpenAI deployment. Enter the full endpoint URL from the Azure portal.',
  },
  ollama:       {
    label:'Ollama (Local)', icon:'🦙',
    models:['llama3.2','llama3.1','codellama','mistral','qwen2.5-coder','deepseek-coder','phi3','gemma2'],
    needsKey:false, needsUrl:true,
    keyPlaceholder:'', urlPlaceholder:'http://localhost:11434',
    hint:'Fully local — no data leaves your machine. Run: <code>ollama serve</code> then <code>ollama pull llama3.2</code>',
  },
  custom:       {
    label:'Custom / Org (Llama, Qwen…)', icon:'⚡',
    // Friendly label → internal model id sent to the endpoint. Same URL + key
    // (from backend .env); only the model id differs between the two.
    models:[{label:'Llama', value:'gpt-4o'}, {label:'Qwen', value:'reasoning-vlm'}],
    needsKey:false, needsUrl:false,          // FROZEN — URL + key come from backend .env
    keyPlaceholder:'', urlPlaceholder:'',
    hint:'🔒 <strong>URL &amp; API key are fixed in the backend .env</strong>.',
  },
};

export const GIT_PROVIDERS = {
  github:            { label:'GitHub Cloud',              icon:'ti-brand-github',    bb:false, enterprise:false },
  github_enterprise: { label:'GitHub Enterprise',         icon:'ti-brand-github',    bb:false, enterprise:true  },
  bitbucket:         { label:'Bitbucket Cloud',            icon:'ti-brand-bitbucket', bb:true,  enterprise:false },
  bitbucket_server:  { label:'Bitbucket Server (On-Prem)', icon:'ti-brand-bitbucket', bb:true,  enterprise:true  },
};

export const LANG_COLORS = {
  JavaScript:'#f1e05a',TypeScript:'#3178c6',Python:'#3572A5',Java:'#b07219',
  Go:'#00ADD8',Ruby:'#701516',Kotlin:'#A97BFF',Rust:'#dea584',
  C:'#555555',Scala:'#c22d40',Shell:'#89e051'
};

// Default LLM-judge panel — 3 independent judges (matches backend default).
export function defaultJudges() {
  return [
    { provider: 'anthropic', model: 'claude-sonnet-4-6' },
    { provider: 'anthropic', model: 'claude-sonnet-4-6' },
    { provider: 'anthropic', model: 'claude-haiku-4-5-20251001' },
  ];
}

// ── Initial state factory ─────────────────────────────────────────────────────
export function createInitialState() {
  return {
    provider: localStorage.getItem('provider') || 'github',
    authMode: localStorage.getItem('authMode') || 'token',
    token: localStorage.getItem('token') || '',
    username: localStorage.getItem('username') || '',
    password: '',
    workspace: localStorage.getItem('workspace') || '',
    projectKey: localStorage.getItem('projectKey') || '',
    baseUrl: localStorage.getItem('baseUrl') || '',
    userInfo: null,
    ciaaRole: localStorage.getItem('ciaaRole') || null,
    ciaaPerms: JSON.parse(localStorage.getItem('ciaaPerms') || 'null') || null,
    backendUrl: localStorage.getItem('backendUrl') || 'http://localhost:8080',
    backendKey: localStorage.getItem('backendKey') || '',
    modelProvider: localStorage.getItem('modelProvider') || 'anthropic',
    modelName: localStorage.getItem('modelName') || 'claude-sonnet-4-6',
    modelApiKey: localStorage.getItem('modelApiKey') || '',
    modelBaseUrl: localStorage.getItem('modelBaseUrl') || '',
    modelApiVer: localStorage.getItem('modelApiVer') || '2024-08-01-preview',
    judges: JSON.parse(localStorage.getItem('judges') || 'null') || defaultJudges(),
    deepScan: false,
    functionalDocs: JSON.parse(localStorage.getItem('functionalDocs') || '[]'),
    repos: [],
    primaryRepo: null,
    connectedRepos: [],
    targetType: 'pr',
    prs: [], branches: [], commits: [],
    selectedPR: null, sourceBranch: '', targetBranch: '',
    commitSha: '',
    report: null,
    lastRequestId: null,
    diffText: '',
    history: JSON.parse(localStorage.getItem('analysisHistory') || '[]'),
    analysisRequested: false,
    runNonce: 0,            // bumped per analysis run to force a fresh RunningView mount
  };
}

export function saveState(state) {
  const keys = ['provider','authMode','token','username','workspace','projectKey','baseUrl',
    'modelProvider','modelName','modelApiKey','modelBaseUrl','modelApiVer',
    'backendUrl','backendKey'];
  keys.forEach(k => {
    if (state[k] !== undefined && state[k] !== null) localStorage.setItem(k, state[k]);
  });
  if (state.ciaaRole) localStorage.setItem('ciaaRole', state.ciaaRole);
  if (state.ciaaPerms) localStorage.setItem('ciaaPerms', JSON.stringify(state.ciaaPerms));
  if (Array.isArray(state.judges)) localStorage.setItem('judges', JSON.stringify(state.judges));
  if (Array.isArray(state.functionalDocs)) localStorage.setItem('functionalDocs', JSON.stringify(state.functionalDocs));
}

// ── Helpers ───────────────────────────────────────────────────────────────────
export function repoName(r) { return r ? (r.full_name || r.slug || r.name || '') : ''; }
export function shortName(r) { const n = repoName(r); return n.includes('/') ? n.split('/')[1] : n; }
export function repoLang(r)  { return r ? (r.language || '') : ''; }
export function prNum(p)    { return `#${p.number || p.id}`; }
export function prTitle(p)  { return p.title || ''; }
export function prHead(p)   { return (p.head?.ref) || (p.source?.branch?.name) || ''; }
export function prBase(p)   { return (p.base?.ref) || (p.destination?.branch?.name) || ''; }
export function prAuthor(p) { return p.user?.login || p.author?.login || p.author?.display_name || p.author?.nickname || ''; }
export function branchName(b) { return b.name || ''; }
export function commitSha(c) { const sha = c.sha || c.hash || c.id || ''; return sha.slice(0, 7); }
export function commitMsg(c) { return c.commit?.message || c.message || ''; }
export function commitAuthor(c) { return c.commit?.author?.name || c.author?.login || c.author?.user?.display_name || c.author?.raw || ''; }
export function escHtml(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
export function fmtDuration(durationS, model) {
  const d = durationS || 0;
  const isStatic = (model || '').toLowerCase() === 'static';
  if (d < 0.01) return isStatic ? '⚡ instant' : (d > 0 ? '<0.01s' : '—');
  return d.toFixed(2) + 's';
}

// Classify how an agent ran: deterministic static, LLM, or fallback (LLM failed).
// model: e.g. 'anthropic/claude-…', 'static', or '' ; opts.fallback/opts.completed.
export function agentEngine(model, opts = {}) {
  const m = (model || '').toLowerCase()
  const FALLBACK = { label: 'fallback', color: '#9a6a00', bg: '#fff8e6', border: '#f0c000',
    title: 'LLM call failed or was skipped — used static rules. Check the model key / budget.' }
  const STATIC = { label: 'static', color: '#1a6cf6', bg: '#eef4ff', border: '#cfe0ff',
    title: 'Deterministic static analysis — no LLM, zero tokens (fast & reproducible by design).' }
  const LLM = { label: 'LLM', color: '#7c3aed', bg: '#f5f0ff', border: '#e2d6fb',
    title: 'Analysed by the LLM' + (model ? ' (' + model + ')' : '') }
  if (opts.fallback) return FALLBACK
  if (m === 'static') return STATIC
  if (m) return LLM
  return opts.completed ? FALLBACK : null   // completed with no model = fell back; else pending
}

export function canPostToGit(state) {
  if (!state.backendKey || !state.backendUrl) return false;
  if (!state.ciaaPerms) return true;
  return !!state.ciaaPerms.can_comment;
}

export function canOverrideGate(state) {
  if (!state.ciaaPerms) return true;
  return !!state.ciaaPerms.can_override;
}

// Admin / Super Admin only — drives visibility of the user-management screen.
// Permissive when the role is unknown (skip_auth / not connected); the backend
// still enforces the user:manage permission + role hierarchy on every call.
export function canManageUsers(state) {
  if (!state.ciaaPerms) return true;
  return (state.ciaaPerms.permissions || []).includes('user:manage');
}
