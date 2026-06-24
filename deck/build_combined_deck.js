// Combined overview deck (4 slides):
//   1. Security & Vulnerability Coverage
//   2. OWASP Top 10 (deep dive, rule-based vs LLM)
//   3. Compliance, Data Protection & Governance
//   4. Guardrails & Safety Controls (Gate · LLM · RBAC · SEC · LDAP-planned)
// Run: NODE_PATH="$(npm root -g)" node deck/build_combined_deck.js
const pptxgen = require("pptxgenjs");
const p = new pptxgen();
p.defineLayout({ name: "W", width: 13.333, height: 7.5 });
p.layout = "W";

const NAVY = "1B2A5E", RED = "C8102E", INK = "1F2733", MUTE = "6B7686",
      LINE = "E4E8EF", CARDBG = "F7F9FC", ICE = "EAF0FB", AMBER = "B5650D",
      DET = "0E7C5A", HEU = "B5650D", LLM = "7C3AED";
const DOT = { det: DET, heu: HEU, llm: LLM };

function header(s, title, sub, subW = 12.1) {
  s.addText(title, { x: 0.6, y: 0.42, w: 12.1, h: 0.58, fontFace: "Calibri", fontSize: 31, bold: true, color: NAVY });
  s.addText(sub, { x: 0.62, y: 1.04, w: subW, h: 0.34, fontFace: "Calibri", fontSize: 13, italic: true, color: MUTE });
}
function footer(s, runs) {
  const fy = 6.72;
  s.addShape(p.ShapeType.roundRect, { x: 0.6, y: fy, w: 12.13, h: 0.48, rectRadius: 0.08, fill: { color: ICE }, line: { type: "none" } });
  s.addText(runs, { x: 0.8, y: fy, w: 11.8, h: 0.48, valign: "middle", margin: 0, fontFace: "Calibri", fontSize: 10 });
}

// ── Generic card-grid renderer (3-col) ──
const COLX = [0.6, 4.74, 8.88], CW = 3.85, CH = 2.30, ROWY = [1.70, 4.16];
function card(s, x, y, c) {
  s.addShape(p.ShapeType.roundRect, { x, y, w: CW, h: CH, rectRadius: 0.08, fill: { color: CARDBG }, line: { color: LINE, width: 1 } });
  s.addShape(p.ShapeType.roundRect, { x: x + 0.24, y: y + 0.24, w: 1.35, h: 0.52, rectRadius: 0.08, fill: { color: c.tagColor }, line: { type: "none" } });
  s.addText(c.tag, { x: x + 0.24, y: y + 0.24, w: 1.35, h: 0.52, align: "center", valign: "middle", fontFace: "Calibri", fontSize: 12, bold: true, color: "FFFFFF" });
  s.addText(c.title, { x: x + 0.24, y: y + 0.88, w: CW - 0.48, h: 0.38, margin: 0, valign: "top", fontFace: "Calibri", fontSize: 13.5, bold: true, color: INK });
  s.addText(c.body, { x: x + 0.24, y: y + 1.26, w: CW - 0.48, h: 0.66, margin: 0, valign: "top", fontFace: "Calibri", fontSize: 9.3, color: MUTE });
  if (c.foot) s.addText(c.foot, { x: x + 0.24, y: y + CH - 0.40, w: CW - 0.48, h: 0.3, margin: 0, valign: "middle", fontFace: "Calibri", fontSize: 9, italic: true, color: c.footColor || NAVY });
}
function grid(s, cards) { cards.forEach((c, i) => card(s, COLX[i % 3], ROWY[Math.floor(i / 3)], c)); }

// ════════ Slide 1 — Security & Vulnerability ════════
let s = p.addSlide(); s.background = { color: "FFFFFF" };
header(s, "Security & Vulnerability Coverage", "Standards and checks enforced on every pull request — Code Analysis & Review");
grid(s, [
  { tag: "OWASP", tagColor: RED, title: "OWASP Top 10 (2021)", foot: "Security · Taint · IaC · SCA   ·   BLOCK",
    body: "Each finding tagged to an OWASP category. 6/10 rule-based (injection, crypto, misconfig, components, auth, SSRF); rest LLM-assisted." },
  { tag: "CWE", tagColor: RED, title: "CWE-classified findings", foot: "Security Review   ·   BLOCK",
    body: "Every finding mapped to its CWE id — injection, auth & crypto weaknesses." },
  { tag: "CVE", tagColor: RED, title: "CVE / SCA (OSV)", foot: "Dependency SCA   ·   BLOCK",
    body: "Known CVEs in dependencies via OSV · CVSS severity · GHSA/CVE ids. Maven · NuGet · PyPI · npm." },
  { tag: "KEY", tagColor: NAVY, title: "Secrets & Credentials", foot: "Secrets / Entropy   ·   BLOCK",
    body: "Hardcoded keys/tokens via Shannon entropy + known key prefixes." },
  { tag: "TAINT", tagColor: NAVY, title: "Injection & SSRF (Taint)", foot: "Taint Analysis   ·   BLOCK",
    body: "User input traced source→sink: SQLi, XSS, path traversal, SSRF." },
  { tag: "IaC", tagColor: NAVY, title: "Infrastructure as Code", foot: "IaC Scan   ·   BLOCK",
    body: "Terraform / Kubernetes / Docker misconfig — public buckets, wildcard IAM, privileged containers." },
]);
footer(s, [{ text: "Critical security findings, confirmed secrets and known CVEs BLOCK the merge gate — rule-based, not advisory.", options: { color: INK } }]);

// ════════ Slide 2 — OWASP deep dive ════════
s = p.addSlide(); s.background = { color: "FFFFFF" };
header(s, "OWASP Top 10 — Detection Depth", "How each category is detected — rule-based, heuristic, or LLM-assisted", 7.5);
[["det", "Rule-based", 8.30], ["heu", "Heuristic", 9.80], ["llm", "LLM-assisted", 11.05]].forEach(([k, lab, lx]) => {
  s.addShape(p.ShapeType.ellipse, { x: lx, y: 1.12, w: 0.15, h: 0.15, fill: { color: DOT[k] }, line: { type: "none" } });
  s.addText(lab, { x: lx + 0.2, y: 1.04, w: 1.5, h: 0.3, margin: 0, valign: "middle", fontFace: "Calibri", fontSize: 10.5, color: MUTE });
});
const owasp = [
  ["A01", "Broken Access Control", "Security Review · Taint Analysis", "llm"],
  ["A02", "Cryptographic Failures", "Security Review · Data Privacy", "det"],
  ["A03", "Injection", "Taint Analysis (source→sink) · Security Review", "det"],
  ["A04", "Insecure Design", "Security Review · AST Analysis", "llm"],
  ["A05", "Security Misconfiguration", "IaC Scan · Secrets / Entropy", "det"],
  ["A06", "Vulnerable & Outdated Components", "Dependency SCA (OSV CVE) · License Compliance", "det"],
  ["A07", "Identification & Auth Failures", "Security Review · Secrets / Entropy", "det"],
  ["A08", "Software & Data Integrity Failures", "Schema Change · Dependency · Interface / API", "llm"],
  ["A09", "Logging & Monitoring Failures", "Observability", "heu"],
  ["A10", "Server-Side Request Forgery (SSRF)", "Taint Analysis · Security Review", "det"],
];
{ const CX = [0.6, 6.92], COLW = 5.82, TOP = 1.62, RH = 1.02;
  owasp.forEach((r, i) => {
    const x = CX[i < 5 ? 0 : 1], y = TOP + (i % 5) * RH;
    s.addShape(p.ShapeType.roundRect, { x, y, w: COLW, h: 0.86, rectRadius: 0.08, fill: { color: CARDBG }, line: { color: LINE, width: 1 } });
    s.addShape(p.ShapeType.roundRect, { x: x + 0.16, y: y + 0.18, w: 0.84, h: 0.5, rectRadius: 0.08, fill: { color: RED }, line: { type: "none" } });
    s.addText(r[0], { x: x + 0.16, y: y + 0.18, w: 0.84, h: 0.5, align: "center", valign: "middle", fontFace: "Calibri", fontSize: 15, bold: true, color: "FFFFFF" });
    s.addText(r[1], { x: x + 1.14, y: y + 0.12, w: COLW - 1.62, h: 0.34, margin: 0, fontFace: "Calibri", fontSize: 14.5, bold: true, color: INK });
    s.addShape(p.ShapeType.ellipse, { x: x + COLW - 0.36, y: y + 0.18, w: 0.16, h: 0.16, fill: { color: DOT[r[3]] }, line: { type: "none" } });
    s.addText([{ text: "Covered by  ", options: { color: MUTE } }, { text: r[2], options: { color: NAVY, bold: true } }],
      { x: x + 1.14, y: y + 0.45, w: COLW - 1.28, h: 0.3, margin: 0, fontFace: "Calibri", fontSize: 10.5 });
  });
}

// ════════ Slide 3 — Compliance & Governance ════════
s = p.addSlide(); s.background = { color: "FFFFFF" };
header(s, "Compliance, Data Protection & Governance", "Regulatory and policy controls enforced alongside the security suite");
grid(s, [
  { tag: "PCI-DSS", tagColor: RED, title: "PCI-DSS", foot: "Data Privacy   ·   HOLD", body: "Cardholder / payment data handled or logged without protection." },
  { tag: "GDPR", tagColor: RED, title: "GDPR", foot: "Data Privacy   ·   HOLD", body: "EU personal data — unencrypted PII storage or transmission." },
  { tag: "PDPA", tagColor: RED, title: "PDPA", foot: "Data Privacy   ·   HOLD", body: "Regional (SG / MY) personal-data protection for PII handling." },
  { tag: "SPDX", tagColor: NAVY, title: "OSS License Compliance", foot: "License Compliance   ·   BLOCK", body: "Copyleft (GPL / LGPL / AGPL / MPL) vs permissive (MIT / Apache / BSD); SPDX ids. Copyleft introduction blocks." },
  { tag: "DB", tagColor: NAVY, title: "Schema / Migration Safety", foot: "Schema Change   ·   BLOCK", body: "Destructive & irreversible DB changes — Flyway / Liquibase / .sql." },
  { tag: "GOV", tagColor: NAVY, title: "Governance & Audit", foot: "Gate Policy · RBAC", body: "Rule-based gate (BLOCK / HOLD), role-based access, full audit log, ELK usage telemetry." },
]);
footer(s, [{ text: "Data-protection breaches hold the gate; copyleft and destructive migrations block it — every decision is logged and auditable.", options: { color: INK } }]);

// ════════ Slide 4 — Guardrails (Gate · LLM · RBAC · SEC · LDAP-planned) ════════
s = p.addSlide(); s.background = { color: "FFFFFF" };
header(s, "Guardrails & Safety Controls", "What keeps the framework safe to trust in a regulated, air-gapped bank network — the LLM advises, the rules decide");
const guards = [
  { tag: "GATE", tagColor: RED, title: "Deterministic gate override", foot: "governance/gate_policy.py",
    body: "Hard rules force BLOCK/HOLD regardless of the model — secrets, known CVEs, confirmed-critical & taint can't be approved away. Policy only raises the gate, never lowers it." },
  { tag: "LLM", tagColor: NAVY, title: "Graceful LLM degradation", foot: "base_agent · llm_client",
    body: "Deterministic regex SAST fallback if the model is down; JSON & reasoning-model recovery so agents never silently fail; retries with back-off, per-agent token budgets, bounded concurrency." },
  { tag: "JUDGE", tagColor: NAVY, title: "Independent judge validation", foot: "evaluation/judge.py · panel",
    body: "A panel of independent LLM judges scores every agent's output against a fixed rubric — completeness, precision, severity accuracy, specificity — and flags consensus-missed critical gaps." },
  { tag: "RBAC", tagColor: NAVY, title: "Access control & audit", foot: "governance/rbac.py · audit log",
    body: "Role-based access — only reviewers override the gate or post to a PR. Every decision & override logged; super-admin-gated settings; ELK usage telemetry." },
  { tag: "SEC", tagColor: NAVY, title: "Secrets & supply-chain hygiene", foot: "frozen env keys · CA bundle",
    body: "API keys stay server-side (Bearer header, never in the URL). Secrets / CVEs / copyleft block the gate. Corporate-CA TLS to OSV & Artifactory; no external CDNs (fully air-gapped)." },
  { tag: "LDAP", tagColor: AMBER, title: "LDAP / directory integration", foot: "Planned — enterprise SSO", footColor: AMBER,
    body: "PLANNED: authenticate users against the corporate LDAP / Active Directory and map AD groups to roles, so access follows the bank's existing identity governance." },
];
grid(s, guards);   // full 3×2 grid (Gate · LLM · Judge · RBAC · SEC · LDAP)
footer(s, [
  { text: "Core principle:  ", options: { bold: true, color: NAVY } },
  { text: "the model proposes, rule-based controls dispose — no single LLM call can approve a PR that carries a secret, a known CVE, or a confirmed critical defect.", options: { color: INK } },
]);

p.writeFile({ fileName: "deck/Code_Analysis_Review_Overview.pptx" })
 .then(f => console.log("wrote", f));
