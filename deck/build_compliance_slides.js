// Two-slide deck: every compliance & security standard Code Analysis & Review enforces.
// Run: NODE_PATH="$(npm root -g)" node deck/build_compliance_slides.js
const pptxgen = require("pptxgenjs");
const p = new pptxgen();
p.defineLayout({ name: "W", width: 13.333, height: 7.5 });
p.layout = "W";

const NAVY = "1B2A5E", RED = "C8102E", INK = "1F2733", MUTE = "6B7686",
      LINE = "E4E8EF", CARDBG = "F7F9FC", ICE = "EAF0FB", GREEN = "0E7C5A";

// 3-col x 2-row card grid
const COLX = [0.6, 4.74, 8.88], CW = 3.85, CH = 2.30;
const ROWY = [1.70, 4.16];

function header(s, title, sub) {
  s.addText(title, { x: 0.6, y: 0.42, w: 12.1, h: 0.58, fontFace: "Calibri", fontSize: 31, bold: true, color: NAVY });
  s.addText(sub,   { x: 0.62, y: 1.04, w: 12.1, h: 0.34, fontFace: "Calibri", fontSize: 13.5, italic: true, color: MUTE });
}

function card(s, i, { tag, tagColor, title, body, agent, gate }) {
  const x = COLX[i % 3], y = ROWY[Math.floor(i / 3)];
  s.addShape(p.ShapeType.roundRect, { x, y, w: CW, h: CH, rectRadius: 0.08,
    fill: { color: CARDBG }, line: { color: LINE, width: 1 } });
  // tag badge
  s.addShape(p.ShapeType.roundRect, { x: x + 0.24, y: y + 0.24, w: 1.15, h: 0.52, rectRadius: 0.08,
    fill: { color: tagColor }, line: { type: "none" } });
  s.addText(tag, { x: x + 0.24, y: y + 0.24, w: 1.15, h: 0.52, align: "center", valign: "middle",
    fontFace: "Calibri", fontSize: 13, bold: true, color: "FFFFFF" });
  // title
  s.addText(title, { x: x + 0.24, y: y + 0.88, w: CW - 0.48, h: 0.38, margin: 0, valign: "top",
    fontFace: "Calibri", fontSize: 14, bold: true, color: INK });
  // body
  s.addText(body, { x: x + 0.24, y: y + 1.28, w: CW - 0.48, h: 0.6, margin: 0, valign: "top",
    fontFace: "Calibri", fontSize: 9.5, color: MUTE });
  // agent + gate footer
  const gColor = gate === "BLOCK" ? RED : gate === "HOLD" ? "B5650D" : GREEN;
  s.addText([
    { text: agent, options: { color: NAVY, bold: true } },
    { text: gate ? `   ·   Gate: ${gate}` : "", options: { color: gColor, bold: true } },
  ], { x: x + 0.24, y: y + CH - 0.42, w: CW - 0.48, h: 0.3, margin: 0, valign: "middle",
    fontFace: "Calibri", fontSize: 9.5 });
}

function footer(s, text) {
  const fy = 6.72;
  s.addShape(p.ShapeType.roundRect, { x: 0.6, y: fy, w: 12.13, h: 0.48, rectRadius: 0.08,
    fill: { color: ICE }, line: { type: "none" } });
  s.addText(text, { x: 0.8, y: fy, w: 11.8, h: 0.48, valign: "middle", margin: 0,
    fontFace: "Calibri", fontSize: 10, color: INK });
}

// ── Slide 1: Application security & vulnerabilities ──
const s1 = p.addSlide(); s1.background = { color: "FFFFFF" };
header(s1, "Security & Vulnerability Coverage",
  "Standards and checks enforced on every pull request — Code Analysis & Review");
[
  { tag: "OWASP", tagColor: RED,  title: "OWASP Top 10 (2021)",
    body: "Each finding tagged to an OWASP category. 6/10 rule-based (injection, crypto, misconfig, components, auth, SSRF); rest LLM-assisted.",
    agent: "Security · Taint · IaC · SCA", gate: "BLOCK" },
  { tag: "CWE", tagColor: RED,     title: "CWE-classified findings",
    body: "Every finding mapped to its CWE id — injection, auth & crypto weaknesses.",
    agent: "Security Review", gate: "BLOCK" },
  { tag: "CVE", tagColor: RED,     title: "CVE / SCA (OSV)",
    body: "Known CVEs in dependencies via OSV · CVSS severity · GHSA/CVE ids. Maven · NuGet · PyPI · npm.",
    agent: "Dependency SCA", gate: "BLOCK" },
  { tag: "KEY", tagColor: NAVY,    title: "Secrets & Credentials",
    body: "Hardcoded keys/tokens via Shannon entropy + known key prefixes.",
    agent: "Secrets / Entropy", gate: "BLOCK" },
  { tag: "TAINT", tagColor: NAVY,  title: "Injection & SSRF (Taint)",
    body: "User input traced source→sink: SQLi, XSS, path traversal, SSRF.",
    agent: "Taint Analysis", gate: "BLOCK" },
  { tag: "IaC", tagColor: NAVY,    title: "Infrastructure as Code",
    body: "Terraform / Kubernetes / Docker misconfig — public buckets, wildcard IAM, privileged containers.",
    agent: "IaC Scan", gate: "BLOCK" },
].forEach((c, i) => card(s1, i, c));
footer(s1, "Critical security findings, confirmed secrets and known CVEs BLOCK the merge gate — rule-based, not advisory.");

// ── Slide 2: Compliance, data protection & governance ──
const s2 = p.addSlide(); s2.background = { color: "FFFFFF" };
header(s2, "Compliance, Data Protection & Governance",
  "Regulatory and policy controls enforced alongside the security suite");
[
  { tag: "PCI-DSS", tagColor: RED, title: "PCI-DSS",
    body: "Cardholder / payment data handled or logged without protection.",
    agent: "Data Privacy", gate: "HOLD" },
  { tag: "GDPR", tagColor: RED,    title: "GDPR",
    body: "EU personal data — unencrypted PII storage or transmission.",
    agent: "Data Privacy", gate: "HOLD" },
  { tag: "PDPA", tagColor: RED,    title: "PDPA",
    body: "Regional (SG / MY) personal-data protection for PII handling.",
    agent: "Data Privacy", gate: "HOLD" },
  { tag: "SPDX", tagColor: NAVY,   title: "OSS License Compliance",
    body: "Copyleft (GPL / LGPL / AGPL / MPL) vs permissive (MIT / Apache / BSD); SPDX ids. Copyleft introduction blocks.",
    agent: "License Compliance", gate: "BLOCK" },
  { tag: "DB", tagColor: NAVY,     title: "Schema / Migration Safety",
    body: "Destructive & irreversible DB changes — Flyway / Liquibase / .sql.",
    agent: "Schema Change", gate: "BLOCK" },
  { tag: "GOV", tagColor: NAVY,    title: "Governance & Audit",
    body: "Rule-based gate (BLOCK / HOLD), role-based access, full audit log, ELK usage telemetry.",
    agent: "Gate Policy · RBAC", gate: "" },
].forEach((c, i) => card(s2, i, c));
footer(s2, "Data-protection breaches hold the gate; copyleft and destructive migrations block it — every decision is logged and auditable.");

p.writeFile({ fileName: "deck/Compliance_Security_Coverage.pptx" })
 .then(f => console.log("wrote", f));
