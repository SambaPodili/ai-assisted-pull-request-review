// Single-slide deck: OWASP Top 10 (2021) mapped to GTO Pull Request Review Framework agents.
// Run: node deck/build_owasp_slide.js  → deck/OWASP_Review_Coverage.pptx
const pptxgen = require("pptxgenjs");
const p = new pptxgen();
p.defineLayout({ name: "W", width: 13.333, height: 7.5 });
p.layout = "W";

const NAVY = "1B2A5E", RED = "C8102E", INK = "1F2733", MUTE = "6B7686",
      LINE = "E4E8EF", CARDBG = "F7F9FC", ICE = "EAF0FB",
      DET = "0E7C5A", HEU = "B5650D", LLM = "7C3AED";   // deterministic / heuristic / LLM
const DOT = { det: DET, heu: HEU, llm: LLM };

const s = p.addSlide();
s.background = { color: "FFFFFF" };

// ── Header ──
s.addText("Security & Code Review Coverage", {
  x: 0.6, y: 0.42, w: 12.1, h: 0.6, fontFace: "Calibri", fontSize: 32, bold: true, color: NAVY });
s.addText("OWASP Top 10 (2021) — mapped to the agents in GTO Pull Request Review Framework", {
  x: 0.62, y: 1.04, w: 7.5, h: 0.34, fontFace: "Calibri", fontSize: 14, italic: true, color: MUTE });

// ── Legend: detection type (top-right, aligned with the subtitle) ──
[["det", "Rule-based", 8.30], ["heu", "Heuristic", 9.80], ["llm", "LLM-assisted", 11.05]]
  .forEach(([k, lab, lx]) => {
    s.addShape(p.ShapeType.ellipse, { x: lx, y: 1.12, w: 0.15, h: 0.15, fill: { color: DOT[k] }, line: { type: "none" } });
    s.addText(lab, { x: lx + 0.2, y: 1.04, w: 1.5, h: 0.3, margin: 0, valign: "middle",
      fontFace: "Calibri", fontSize: 10.5, color: MUTE });
  });

// ── OWASP rows ──
// [code, name, covered-by, detection: det = deterministic, heu = heuristic, llm = LLM-assisted]
const rows = [
  ["A01", "Broken Access Control",            "Security Review · Taint Analysis",             "llm"],
  ["A02", "Cryptographic Failures",           "Security Review · Data Privacy",               "det"],
  ["A03", "Injection",                        "Taint Analysis (source→sink) · Security Review","det"],
  ["A04", "Insecure Design",                  "Security Review · AST Analysis",               "llm"],
  ["A05", "Security Misconfiguration",        "IaC Scan · Secrets / Entropy",                 "det"],
  ["A06", "Vulnerable & Outdated Components", "Dependency SCA (OSV CVE) · License Compliance","det"],
  ["A07", "Identification & Auth Failures",   "Security Review · Secrets / Entropy",          "det"],
  ["A08", "Software & Data Integrity Failures","Schema Change · Dependency · Interface / API","llm"],
  ["A09", "Logging & Monitoring Failures",    "Observability",                                "heu"],
  ["A10", "Server-Side Request Forgery (SSRF)","Taint Analysis · Security Review",            "det"],
];

const COLX = [0.6, 6.92], COLW = 5.82;
const TOP = 1.62, RH = 1.02;
rows.forEach((r, i) => {
  const col = i < 5 ? 0 : 1, row = i % 5;
  const x = COLX[col], y = TOP + row * RH;
  // card
  s.addShape(p.ShapeType.roundRect, { x, y, w: COLW, h: 0.86, rectRadius: 0.08,
    fill: { color: CARDBG }, line: { color: LINE, width: 1 } });
  // code badge
  s.addShape(p.ShapeType.roundRect, { x: x + 0.16, y: y + 0.18, w: 0.84, h: 0.5, rectRadius: 0.08,
    fill: { color: RED }, line: { type: "none" } });
  s.addText(r[0], { x: x + 0.16, y: y + 0.18, w: 0.84, h: 0.5, align: "center", valign: "middle",
    fontFace: "Calibri", fontSize: 15, bold: true, color: "FFFFFF" });
  // title (narrowed to leave room for the detection dot)
  s.addText(r[1], { x: x + 1.14, y: y + 0.12, w: COLW - 1.62, h: 0.34, margin: 0,
    fontFace: "Calibri", fontSize: 14.5, bold: true, color: INK });
  // detection-type dot (deterministic / heuristic / LLM-assisted)
  s.addShape(p.ShapeType.ellipse, { x: x + COLW - 0.36, y: y + 0.18, w: 0.16, h: 0.16,
    fill: { color: DOT[r[3]] }, line: { type: "none" } });
  // coverage
  s.addText([{ text: "Covered by  ", options: { color: MUTE } },
             { text: r[2], options: { color: NAVY, bold: true } }],
    { x: x + 1.14, y: y + 0.45, w: COLW - 1.28, h: 0.3, margin: 0,
      fontFace: "Calibri", fontSize: 10.5 });
});

// ── Footer band: broader engineering review suite ──
const fy = 6.86;
s.addShape(p.ShapeType.roundRect, { x: 0.6, y: fy, w: 12.13, h: 0.5, rectRadius: 0.08,
  fill: { color: ICE }, line: { type: "none" } });
s.addText([
  { text: "Beyond security — 12+ engineering reviews:  ", options: { bold: true, color: NAVY } },
  { text: "Performance Impact · Test Coverage · QA Scenarios · Reference & Cross-Repo Impact · "
        + "Maintainability · Temporal Risk · FSD Validation · Risk Assessment → enforced Gate",
    options: { color: INK } },
], { x: 0.8, y: fy, w: 11.8, h: 0.5, valign: "middle", margin: 0, fontFace: "Calibri", fontSize: 10.5 });

p.writeFile({ fileName: "deck/OWASP_Review_Coverage.pptx" })
 .then(f => console.log("wrote", f));
