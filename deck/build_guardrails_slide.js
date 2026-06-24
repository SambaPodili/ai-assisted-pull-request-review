// Single slide: the guardrails / safety controls in Code Analysis & Review.
// Run: NODE_PATH="$(npm root -g)" node deck/build_guardrails_slide.js
const pptxgen = require("pptxgenjs");
const p = new pptxgen();
p.defineLayout({ name: "W", width: 13.333, height: 7.5 });
p.layout = "W";

const NAVY = "1B2A5E", RED = "C8102E", INK = "1F2733", MUTE = "6B7686",
      LINE = "E4E8EF", CARDBG = "F7F9FC", ICE = "EAF0FB";

const COLX = [0.6, 4.74, 8.88], CW = 3.85, CH = 2.30;
const ROWY = [1.70, 4.16];

const s = p.addSlide();
s.background = { color: "FFFFFF" };
s.addText("Guardrails & Safety Controls", {
  x: 0.6, y: 0.42, w: 12.1, h: 0.58, fontFace: "Calibri", fontSize: 31, bold: true, color: NAVY });
s.addText("What keeps the framework safe to trust in a regulated, air-gapped bank network — the LLM advises, the rules decide", {
  x: 0.62, y: 1.04, w: 12.1, h: 0.34, fontFace: "Calibri", fontSize: 13, italic: true, color: MUTE });

const cards = [
  { tag: "GATE", title: "Deterministic gate override",
    body: "Hard rules force BLOCK/HOLD regardless of the model — secrets, known CVEs, confirmed-critical & taint can't be approved away. Policy only raises the gate, never lowers it.",
    foot: "governance/gate_policy.py" },
  { tag: "FP", title: "False-positive controls",
    body: "Only verified, in-diff, confirmed findings gate the merge. Unverified (cite a file not in the diff), phantom & speculative findings are display-only, never blockers.",
    foot: "_has_content · confidence weighting" },
  { tag: "LLM", title: "Graceful LLM degradation",
    body: "Deterministic regex SAST fallback if the model is down; JSON & reasoning-model recovery so agents never silently fail; retries with back-off, per-agent token budgets, bounded concurrency.",
    foot: "base_agent · llm_client" },
  { tag: "RBAC", title: "Access control & audit",
    body: "Role-based access — only reviewers override the gate or post to a PR. Every decision & override logged to the audit trail; super-admin-gated settings; ELK usage telemetry.",
    foot: "governance/rbac.py · audit log" },
  { tag: "SEC", title: "Secrets & supply-chain hygiene",
    body: "API keys stay server-side (Bearer header, never in the URL). Secrets / CVEs / copyleft block the gate. Corporate-CA TLS to OSV & Artifactory; no external CDNs (fully air-gapped).",
    foot: "frozen env keys · CA bundle" },
  { tag: "TRUST", title: "Honest failure modes",
    body: "OSV-unreachable is surfaced — never a silent 'no vulnerabilities'. Full deep-scan coverage, no silent file skipping. The enforced gate is frozen/persisted so it can't drift on reload.",
    foot: "OsvUnavailable · gate persistence" },
];

cards.forEach((c, i) => {
  const x = COLX[i % 3], y = ROWY[Math.floor(i / 3)];
  s.addShape(p.ShapeType.roundRect, { x, y, w: CW, h: CH, rectRadius: 0.08,
    fill: { color: CARDBG }, line: { color: LINE, width: 1 } });
  s.addShape(p.ShapeType.roundRect, { x: x + 0.24, y: y + 0.24, w: 1.3, h: 0.52, rectRadius: 0.08,
    fill: { color: i < 3 ? RED : NAVY }, line: { type: "none" } });
  s.addText(c.tag, { x: x + 0.24, y: y + 0.24, w: 1.3, h: 0.52, align: "center", valign: "middle",
    fontFace: "Calibri", fontSize: 12.5, bold: true, color: "FFFFFF" });
  s.addText(c.title, { x: x + 0.24, y: y + 0.88, w: CW - 0.48, h: 0.38, margin: 0, valign: "top",
    fontFace: "Calibri", fontSize: 14, bold: true, color: INK });
  s.addText(c.body, { x: x + 0.24, y: y + 1.26, w: CW - 0.48, h: 0.66, margin: 0, valign: "top",
    fontFace: "Calibri", fontSize: 9.3, color: MUTE });
  s.addText(c.foot, { x: x + 0.24, y: y + CH - 0.40, w: CW - 0.48, h: 0.3, margin: 0, valign: "middle",
    fontFace: "Calibri", fontSize: 9, italic: true, color: NAVY });
});

const fy = 6.72;
s.addShape(p.ShapeType.roundRect, { x: 0.6, y: fy, w: 12.13, h: 0.48, rectRadius: 0.08,
  fill: { color: ICE }, line: { type: "none" } });
s.addText([
  { text: "Core principle:  ", options: { bold: true, color: NAVY } },
  { text: "the model proposes, deterministic rules dispose — no single LLM call can approve a PR that carries a secret, a known CVE, or a confirmed critical defect.",
    options: { color: INK } },
], { x: 0.8, y: fy, w: 11.8, h: 0.48, valign: "middle", margin: 0, fontFace: "Calibri", fontSize: 10 });

p.writeFile({ fileName: "deck/Guardrails_Coverage.pptx" })
 .then(f => console.log("wrote", f));
