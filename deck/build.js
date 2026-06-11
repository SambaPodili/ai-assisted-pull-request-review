const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const fa = require("react-icons/fa");

// ── Palette (Midnight / Ocean tech) ───────────────────────────────────────────
const C = {
  navy:   "0E1A33",   // deep background
  panel:  "16243F",   // dark card
  ink:    "1E293B",   // dark text on light
  muted:  "64748B",   // muted text
  light:  "FFFFFF",
  card:   "F6F8FB",
  border: "E2E8F0",
  teal:   "2DD4BF",
  blue:   "38BDF8",
  indigo: "6366F1",
  green:  "16A34A",
  amber:  "D97706",
  red:    "DC2626",
  ice:    "CADCFC",
};
const HF = "Georgia";        // header font
const BF = "Calibri";        // body font
const MF = "Consolas";

const W = 13.333, H = 7.5, MX = 0.65;

async function icon(Comp, color = "#0E1A33", size = 256) {
  const svg = ReactDOMServer.renderToStaticMarkup(React.createElement(Comp, { color, size: String(size) }));
  const png = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + png.toString("base64");
}

(async () => {
  // Pre-render icons
  const I = {};
  const want = {
    shield: fa.FaShieldAlt, branch: fa.FaCodeBranch, diagram: fa.FaProjectDiagram,
    gauge: fa.FaTachometerAlt, check: fa.FaCheckCircle, x: fa.FaTimesCircle,
    warn: fa.FaExclamationTriangle, robot: fa.FaRobot, db: fa.FaDatabase,
    bug: fa.FaBug, vial: fa.FaVial, scale: fa.FaBalanceScale, net: fa.FaNetworkWired,
    sitemap: fa.FaSitemap, lock: fa.FaLock, bolt: fa.FaBolt, layers: fa.FaLayerGroup,
    users: fa.FaUsers, server: fa.FaServer, search: fa.FaSearch, code: fa.FaFileCode,
    gavel: fa.FaGavel, plug: fa.FaPlug, eye: fa.FaEye, flow: fa.FaStream, cube: fa.FaCube,
    clock: fa.FaHistory, brain: fa.FaBrain,
  };
  for (const [k, comp] of Object.entries(want)) {
    I[k]       = await icon(comp, "#FFFFFF");
    I[k + "_d"] = await icon(comp, "#0E1A33");
  }
  // colored variants
  I.check_g = await icon(fa.FaCheckCircle, "#16A34A");
  I.x_r     = await icon(fa.FaTimesCircle, "#DC2626");
  I.warn_a  = await icon(fa.FaExclamationTriangle, "#D97706");

  const p = new pptxgen();
  p.defineLayout({ name: "W", width: W, height: H });
  p.layout = "W";
  p.author = "CIAA";
  p.title  = "CIAA — Code Impact & Analysis Agent";

  // ── helpers ─────────────────────────────────────────────────────────────────
  const title = (s, t, col = C.ink, sub) => {
    s.addText(t, { x: MX, y: 0.62, w: W - 2 * MX, h: 0.8, fontFace: HF, fontSize: 30, bold: true, color: col, margin: 0 });
    if (sub) s.addText(sub, { x: MX, y: 1.4, w: W - 2 * MX, h: 0.5, fontFace: BF, fontSize: 14, color: C.muted, margin: 0 });
  };
  const eyebrow = (s, t, col, x = MX, y = 0.4, w = 6) =>
    s.addText(t.toUpperCase(), { x, y, w, h: 0.3, fontFace: BF, fontSize: 12, bold: true, color: col, charSpacing: 3, margin: 0 });

  const iconChip = (s, data, x, y, d = 0.62, fill = C.navy) => {
    s.addShape(p.shapes.OVAL, { x, y, w: d, h: d, fill: { color: fill } });
    s.addImage({ data, x: x + d * 0.27, y: y + d * 0.27, w: d * 0.46, h: d * 0.46 });
  };

  // card with icon, heading, body
  const card = (s, x, y, w, h, ic, head, body, accent) => {
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.09, fill: { color: C.card }, line: { color: C.border, width: 1 },
      shadow: { type: "outer", color: "0E1A33", blur: 7, offset: 2, angle: 90, opacity: 0.08 } });
    s.addShape(p.shapes.RECTANGLE, { x, y, w: 0.08, h, fill: { color: accent } });
    iconChip(s, ic, x + 0.26, y + 0.26, 0.6, accent);
    s.addText(head, { x: x + 1.0, y: y + 0.26, w: w - 1.2, h: 0.62, fontFace: HF, fontSize: 14.5, bold: true, color: C.ink, valign: "middle", margin: 0 });
    s.addText(body, { x: x + 0.28, y: y + 1.02, w: w - 0.56, h: h - 1.18, fontFace: BF, fontSize: 11.5, color: C.muted, valign: "top", margin: 0, lineSpacingMultiple: 1.02 });
  };

  // Slide factory that auto-adds a page number (skips the title slide).
  const _add = p.addSlide.bind(p);
  let __pg = 0;
  const mkSlide = () => {
    const sl = _add(); __pg++;
    if (__pg > 1) sl.slideNumber = { x: 12.5, y: 7.04, w: 0.7, h: 0.3, fontFace: BF, fontSize: 9, color: "94A3B8", align: "right" };
    return sl;
  };

  // Left explanatory column shared by the technique deep-dive slides.
  const leftCol = (s, ac, whatIs, detects, why) => {
    const lx = MX, lw = 5.95;
    s.addText("WHAT IT IS", { x: lx, y: 1.95, w: lw, h: 0.3, fontFace: BF, fontSize: 11, bold: true, color: ac, charSpacing: 2, margin: 0 });
    s.addText(whatIs, { x: lx, y: 2.25, w: lw, h: 0.95, fontFace: BF, fontSize: 12.5, color: C.ink, margin: 0, lineSpacingMultiple: 1.06 });
    s.addText("WHAT IT DETECTS", { x: lx, y: 3.3, w: lw, h: 0.3, fontFace: BF, fontSize: 11, bold: true, color: ac, charSpacing: 2, margin: 0 });
    s.addText(detects.map(t => ({ text: t, options: { bullet: { indent: 14 }, breakLine: true } })),
      { x: lx + 0.05, y: 3.62, w: lw - 0.05, h: 1.85, fontFace: BF, fontSize: 12, color: C.ink, margin: 0, lineSpacingMultiple: 1.05, paraSpaceAfter: 6 });
    s.addText("WHY IT MATTERS", { x: lx, y: 5.62, w: lw, h: 0.3, fontFace: BF, fontSize: 11, bold: true, color: ac, charSpacing: 2, margin: 0 });
    s.addText(why, { x: lx, y: 5.92, w: lw, h: 0.9, fontFace: BF, fontSize: 12, italic: true, color: C.muted, margin: 0, lineSpacingMultiple: 1.04 });
  };
  const RX = 7.0, RW = W - MX - RX;   // right visual panel
  const dpanel = (s) => s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: RX, y: 1.95, w: RW, h: 4.85, rectRadius: 0.09, fill: { color: C.card }, line: { color: C.border, width: 1 } });
  const flowBox = (s, x, y, w, h, ac, tag, code, cap) => {
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.07, fill: { color: C.light }, line: { color: ac, width: 1.5 } });
    s.addText(tag, { x: x + 0.18, y: y + 0.12, w: w - 0.3, h: 0.3, fontFace: BF, fontSize: 11, bold: true, color: ac, margin: 0 });
    s.addText(code, { x: x + 0.18, y: y + 0.4, w: w - 0.36, h: 0.34, fontFace: MF, fontSize: 11, color: C.ink, margin: 0 });
    s.addText(cap, { x: x + 0.18, y: y + 0.71, w: w - 0.36, h: 0.28, fontFace: BF, fontSize: 10, italic: true, color: C.muted, margin: 0 });
  };
  const downArrow = (s, cx, y) => s.addText("▼", { x: cx - 0.3, y, w: 0.6, h: 0.3, fontFace: BF, fontSize: 14, color: C.muted, align: "center", margin: 0 });

  // ════════════════════════════ SLIDE 1 — TITLE ════════════════════════════
  let s = mkSlide();
  s.background = { color: C.navy };
  // motif: faint agent-node dots top-right
  for (let i = 0; i < 6; i++)
    s.addShape(p.shapes.OVAL, { x: 10.6 + (i % 3) * 0.62, y: 0.7 + Math.floor(i / 3) * 0.62, w: 0.16, h: 0.16, fill: { color: C.teal, transparency: 30 + i * 8 } });
  s.addShape(p.shapes.RECTANGLE, { x: 0, y: 0, w: 0.18, h: H, fill: { color: C.teal } });
  eyebrow(s, "Enterprise AI Code Governance", C.teal, MX, 1.7, 9);
  s.addText("CIAA", { x: MX, y: 2.05, w: 11, h: 1.0, fontFace: HF, fontSize: 60, bold: true, color: C.light, margin: 0 });
  s.addText("Code Impact & Analysis Agent", { x: MX, y: 3.15, w: 11.5, h: 0.7, fontFace: HF, fontSize: 28, color: C.ice, margin: 0 });
  s.addText("An AI multi-agent framework for Pull-Request review and code-impact analysis —\nbuilt to catch production issues before they merge.",
    { x: MX, y: 3.95, w: 10.5, h: 0.9, fontFace: BF, fontSize: 15, color: "AAB7D4", margin: 0, lineSpacingMultiple: 1.1 });
  // chips
  const chips = ["20+ specialist agents", "Deterministic merge gate", "Self-hosted LLM ready", "GitHub · Bitbucket Server"];
  let cx = MX;
  for (const ch of chips) {
    const wch = 0.32 + ch.length * 0.092;
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: cx, y: 5.55, w: wch, h: 0.46, rectRadius: 0.23, fill: { color: C.panel }, line: { color: "2A3D63", width: 1 } });
    s.addText(ch, { x: cx, y: 5.55, w: wch, h: 0.46, fontFace: BF, fontSize: 11.5, bold: true, color: C.ice, align: "center", valign: "middle", margin: 0 });
    cx += wch + 0.22;
  }

  // ════════════════════════════ SLIDE 2 — PROBLEM ════════════════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "The Problem", C.red);
  title(s, "Reviews don't scale — scanners miss the impact");
  const probs = [
    ["Large PRs overwhelm reviewers", "Changes routinely span 50–100+ files. Humans can't reliably trace cross-cutting impact across modules and services."],
    ["SAST is blind to the change", "SonarQube / Veracode flag code-pattern issues, but not change impact: blast radius, downstream consumers, contract breaks, test adequacy."],
    ["No deterministic merge decision", "Reviews are inconsistent and reviewer-dependent — no auditable, repeatable APPROVE / HOLD / BLOCK gate."],
    ["Regressions reach production", "Breaking API & data-contract changes slip through and surface as incidents after release."],
  ];
  let py = 1.95;
  for (const [h2, b] of probs) {
    iconChip(s, I.warn_a, MX, py + 0.02, 0.5, C.light);
    s.addImage({ data: I.warn_a, x: MX, y: py + 0.03, w: 0.42, h: 0.42 });
    s.addText(h2, { x: MX + 0.62, y: py - 0.05, w: 6.0, h: 0.4, fontFace: HF, fontSize: 15, bold: true, color: C.ink, margin: 0 });
    s.addText(b, { x: MX + 0.62, y: py + 0.34, w: 6.1, h: 0.7, fontFace: BF, fontSize: 11.5, color: C.muted, margin: 0, lineSpacingMultiple: 1.03 });
    py += 1.18;
  }
  // right stat panel
  const sx = 7.7, sw = W - MX - sx;
  s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: sx, y: 1.95, w: sw, h: 4.6, rectRadius: 0.1, fill: { color: C.navy } });
  const stats = [["100+", "files in a single PR — beyond human review capacity"], ["Blind", "to cross-repo blast radius & downstream consumers"], ["Inconsistent", "merge decisions with no audit trail"]];
  let stx = 2.25;
  for (const [n, l] of stats) {
    s.addText(n, { x: sx + 0.4, y: stx, w: sw - 0.8, h: 0.6, fontFace: HF, fontSize: 30, bold: true, color: C.teal, margin: 0 });
    s.addText(l, { x: sx + 0.4, y: stx + 0.62, w: sw - 0.8, h: 0.6, fontFace: BF, fontSize: 12, color: C.ice, margin: 0, lineSpacingMultiple: 1.02 });
    stx += 1.42;
  }

  // ════════════════════════════ SLIDE 3 — SOLUTION ════════════════════════════
  s = mkSlide(); s.background = { color: C.navy };
  eyebrow(s, "The Solution", C.teal);
  s.addText("A multi-agent reviewer that understands the change", { x: MX, y: 0.85, w: W - 2 * MX, h: 0.8, fontFace: HF, fontSize: 28, bold: true, color: C.light, margin: 0 });
  s.addText("20+ specialized AI agents analyze every PR across security, quality, tests, contracts and downstream impact — then a deterministic policy returns one auditable decision.",
    { x: MX, y: 1.62, w: 11.7, h: 0.6, fontFace: BF, fontSize: 13.5, color: "AAB7D4", margin: 0, lineSpacingMultiple: 1.05 });
  const pillars = [
    [I.branch, "Deep PR Review", "Security, code quality, test adequacy, compliance and dependency/CVE scanning on the diff.", C.blue],
    [I.diagram, "Real Impact Analysis", "Blast radius, cross-repo references, downstream consumers, and contract / serialization changes.", C.teal],
    [I.gavel, "Deterministic Governance", "Auditable APPROVE / HOLD / BLOCK gate, evidence per finding, and a reviewer feedback loop.", C.indigo],
  ];
  const pw = (W - 2 * MX - 2 * 0.4) / 3;
  pillars.forEach(([ic, h2, b, ac], i) => {
    const x = MX + i * (pw + 0.4);
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y: 2.5, w: pw, h: 3.5, rectRadius: 0.1, fill: { color: C.panel }, line: { color: "2A3D63", width: 1 } });
    iconChip(s, ic, x + 0.45, 2.9, 0.78, ac);
    s.addText(h2, { x: x + 0.4, y: 3.95, w: pw - 0.8, h: 0.5, fontFace: HF, fontSize: 17, bold: true, color: C.light, margin: 0 });
    s.addText(b, { x: x + 0.4, y: 4.5, w: pw - 0.8, h: 1.3, fontFace: BF, fontSize: 12.5, color: C.ice, margin: 0, lineSpacingMultiple: 1.08 });
  });
  s.addText("Works with GitHub · GitHub Enterprise · Bitbucket Cloud & Server (Data Center)   |   Any LLM: Anthropic · OpenAI · Azure · Ollama · self-hosted",
    { x: MX, y: 6.35, w: W - 2 * MX, h: 0.5, fontFace: BF, fontSize: 11.5, italic: true, color: C.teal, align: "center", margin: 0 });

  // ════════════════════════════ SLIDE 4 — PIPELINE ════════════════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "How it works", C.blue);
  title(s, "One pipeline · four phases · one decision");
  const nodes = [
    [I.code, "Ingest", "Fetch diff, parse hunks, rank changed symbols", C.muted],
    [I.shield, "Phase 1", "Code review + Security review", C.blue],
    [I.layers, "Phase 1b", "Deep scan: AST, taint, secrets, schema, references, performance, privacy", C.teal],
    [I.diagram, "Phase 2–3", "Dependency, tests, interface, risk, remediation", C.indigo],
    [I.gauge, "Gate", "Deterministic APPROVE / HOLD / BLOCK", C.green],
  ];
  const nw = 2.18, ngap = (W - 2 * MX - 5 * nw) / 4, ny = 2.5, nh = 2.7;
  nodes.forEach(([ic, h2, b, ac], i) => {
    const x = MX + i * (nw + ngap);
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y: ny, w: nw, h: nh, rectRadius: 0.1, fill: { color: C.card }, line: { color: C.border, width: 1 } });
    s.addShape(p.shapes.RECTANGLE, { x, y: ny, w: nw, h: 0.12, fill: { color: ac } });
    iconChip(s, ic, x + (nw - 0.7) / 2, ny + 0.32, 0.7, ac);
    s.addText(h2, { x, y: ny + 1.12, w: nw, h: 0.4, fontFace: HF, fontSize: 15, bold: true, color: C.ink, align: "center", margin: 0 });
    s.addText(b, { x: x + 0.16, y: ny + 1.55, w: nw - 0.32, h: 1.0, fontFace: BF, fontSize: 10.5, color: C.muted, align: "center", margin: 0, lineSpacingMultiple: 1.0 });
    if (i < nodes.length - 1) s.addText("›", { x: x + nw, y: ny + nh / 2 - 0.45, w: ngap, h: 0.9, fontFace: HF, fontSize: 30, bold: true, color: C.teal, align: "center", valign: "middle", margin: 0 });
  });
  s.addText([
    { text: "Deterministic", options: { bold: true, color: C.ink } },
    { text: " (temperature 0 — same diff, same verdict)     •     ", options: { color: C.muted } },
    { text: "Evidence-guarded", options: { bold: true, color: C.ink } },
    { text: " (findings cite file:line in the diff)     •     ", options: { color: C.muted } },
    { text: "Correlated", options: { bold: true, color: C.ink } },
    { text: " (cross-agent dedupe → ranked Top Issues)     •     ", options: { color: C.muted } },
    { text: "Self-improving", options: { bold: true, color: C.ink } },
    { text: " (reviewer feedback suppresses repeat false positives)", options: { color: C.muted } },
  ], { x: MX, y: 5.95, w: W - 2 * MX, h: 0.6, fontFace: BF, fontSize: 12, align: "center", margin: 0 });

  // ════════════════ SLIDE 5 — PR REVIEW COVERAGE ════════════════
  const grid = (heading, eyeb, eyeCol, items) => {
    const sl = mkSlide(); sl.background = { color: C.light };
    eyebrow(sl, eyeb, eyeCol);
    title(sl, heading);
    const cw = (W - 2 * MX - 2 * 0.4) / 3, ch = 2.32, gy = 0.4;
    items.forEach(([ic, h2, b, ac], i) => {
      const col = i % 3, row = Math.floor(i / 3);
      card(sl, MX + col * (cw + 0.4), 1.95 + row * (ch + gy), cw, ch, ic, h2, b, ac);
    });
    return sl;
  };

  grid("Covered in Pull-Request Review", "Pull-Request Review", C.blue, [
    [I.shield, "Security review", "OWASP Top 10, taint / data-flow analysis, hardcoded secrets & entropy, IaC misconfiguration.", C.red],
    [I.code, "Code quality", "AST analysis: dead code, null-dereference risk, type confusion, complexity & maintainability.", C.blue],
    [I.vial, "Test adequacy", "Method-level scenario gaps, repo-aware (existing tests count), hollow-test detection.", C.green],
    [I.bug, "Dependencies & SCA", "OSV CVE lookup, Maven pom.xml direct-dependency scan, vulnerable-version flags.", C.amber],
    [I.db, "DB schema safety", "Destructive / irreversible migration detection, rollback SQL, DBA sign-off prompts.", C.indigo],
    [I.scale, "Compliance mapping", "Pass / fail mapped to OWASP, PCI-DSS 4.0 and CWE Top 25 — with code evidence.", C.teal],
  ]);

  // ════════════════ SLIDE 6 — IMPACT ANALYSIS COVERAGE ════════════════
  grid("Covered in Impact Analysis", "Impact Analysis", C.teal, [
    [I.net, "Blast radius", "Graph traversal (NetworkX / Neo4j) or derived from real signals — scored 0–100 with a breakdown.", C.teal],
    [I.search, "Cross-repo references", "Finds call-sites in dependent repos: warm mirror → provider code search → shallow clone + grep.", C.blue],
    [I.users, "Downstream consumers", "Exact call-sites a breaking change will break, each with its likely runtime failure mode.", C.red],
    [I.plug, "Interface & contracts", "Breaking changes (removed / renamed / retyped) + additive data-model fields + serialization config.", C.indigo],
    [I.sitemap, "Call / reference graph", "Layered and force views, folder grouping and focus-on-hover to read large graphs clearly.", C.amber],
    [I.cube, "Capability mapping", "Maps changed files to the owning business capabilities and teams to loop into the review.", C.green],
  ]);

  // ════════════════ SLIDE 7 — TECHNIQUES & TERMINOLOGY ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Techniques & terminology", C.teal);
  title(s, "The analysis techniques, in plain English");
  const terms = [
    [I.code, "AST Analysis", C.blue, "Parses real syntax trees (tree-sitter: Java, Kotlin, C#, JS/TS, Go + Python) to measure exact complexity and flag dead code, null risk and type confusion — without running the code."],
    [I.server, "IaC Scanning", C.teal, "Checks infrastructure-as-code (Terraform, Kubernetes, Docker) for security misconfigurations before deploy."],
    [I.lock, "Entropy / Secrets", C.red, "Shannon-entropy scoring plus known key prefixes to catch hardcoded credentials, API keys and tokens."],
    [I.bug, "SCA — Composition Analysis", C.amber, "Scans third-party dependencies against the OSV database for known CVEs (vulnerable versions)."],
    [I.flow, "Taint Analysis", C.indigo, "Follows untrusted input (source) to a dangerous operation (sink) to expose injection & data-flow flaws (CWE)."],
    [I.net, "Blast Radius", C.teal, "Graph traversal that scores how far a change ripples across files and services, from 0 to 100."],
    [I.clock, "Temporal Risk", C.amber, "Mines change history for hot files, escalating churn and security erosion over time."],
    [I.gavel, "LLM-as-Judge", C.indigo, "Independent AI judges grade every agent's output for completeness, precision and specificity."],
  ];
  const dcw = (W - 2 * MX - 0.4) / 2, dch = 1.12, dgx = 0.4, dgy = 0.14, dY = 1.72;
  terms.forEach(([ic, term, ac, def], i) => {
    const col = i % 2, row = Math.floor(i / 2);
    const x = MX + col * (dcw + dgx), y = dY + row * (dch + dgy);
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y, w: dcw, h: dch, rectRadius: 0.08, fill: { color: C.card }, line: { color: C.border, width: 1 } });
    s.addShape(p.shapes.RECTANGLE, { x, y, w: 0.07, h: dch, fill: { color: ac } });
    iconChip(s, ic, x + 0.24, y + (dch - 0.5) / 2, 0.5, ac);
    s.addText(term, { x: x + 0.88, y: y + 0.14, w: dcw - 1.05, h: 0.34, fontFace: HF, fontSize: 13.5, bold: true, color: C.ink, margin: 0 });
    s.addText(def, { x: x + 0.88, y: y + 0.47, w: dcw - 1.1, h: 0.58, fontFace: BF, fontSize: 10.3, color: C.muted, margin: 0, lineSpacingMultiple: 1.0 });
  });
  s.addText([
    { text: "Also: ", options: { bold: true, color: C.ink } },
    { text: "Reference-impact", options: { bold: true, color: C.ink } }, { text: " (cross-repo call-site tracing)  ·  ", options: { color: C.muted } },
    { text: "Cross-agent correlation", options: { bold: true, color: C.ink } }, { text: " (agreement boosts confidence, dedupes to Top Issues)  ·  ", options: { color: C.muted } },
    { text: "Deterministic temp-0", options: { bold: true, color: C.ink } }, { text: " inference  ·  ", options: { color: C.muted } },
    { text: "Circuit breakers", options: { bold: true, color: C.ink } }, { text: " (auto-isolate a failing agent)", options: { color: C.muted } },
  ], { x: MX, y: 6.95, w: W - 2 * MX, h: 0.45, fontFace: BF, fontSize: 10.5, align: "center", margin: 0 });

  // ════════════════ SLIDE 8 — DEEP DIVE: TAINT ANALYSIS ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Deep dive · Technique", C.indigo);
  title(s, "Taint Analysis — source → sink data flow");
  leftCol(s, C.indigo,
    "Taint analysis tracks data from where it enters the system to where it is used, following it through assignments and method calls.",
    ["SQL / command / LDAP injection", "Cross-site scripting (XSS)", "Path traversal & SSRF", "Any unsanitised value reaching a sensitive sink"],
    "Most high-severity breaches are a tainted value reaching a dangerous sink. CIAA shows the exact source → sink path with its CWE.");
  dpanel(s);
  { const bx = RX + 0.55, bw = RW - 1.1, cxp = RX + RW / 2;
    flowBox(s, bx, 2.35, bw, 1.05, C.green,  "①  SOURCE",       'request.getParameter("id")', "untrusted user input");
    downArrow(s, cxp, 3.48);
    flowBox(s, bx, 3.8, bw, 1.05, C.amber,  "②  PROPAGATION",  'String q = "...WHERE id=" + id;', "taint flows into the query");
    downArrow(s, cxp, 4.93);
    flowBox(s, bx, 5.25, bw, 1.05,  C.red,   "③  SINK",         "stmt.execute(q)", "SQL injection — CWE-89"); }

  // ════════════════ SLIDE 9 — DEEP DIVE: AST ANALYSIS ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Deep dive · Technique", C.blue);
  title(s, "AST Analysis — understanding code structure");
  leftCol(s, C.blue,
    "Tree-sitter parses source into a real syntax tree (Java, Kotlin, C#, JS/TS, Go — plus Python via stdlib ast), which CIAA walks to measure exact complexity without executing the code.",
    ["Unreachable / dead code", "Null-dereference & none-type risk", "Type confusion / unsafe casts", "Complexity & deep-nesting hot-spots"],
    "Catches latent bugs a regex linter can't see, because it reasons over structure — not just text.");
  dpanel(s);
  // code snippet (dark)
  s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: RX + 0.4, y: 2.25, w: RW - 0.8, h: 1.4, rectRadius: 0.07, fill: { color: C.navy } });
  s.addText([
    { text: "int risk(int x) {", options: { breakLine: true } },
    { text: "    if (x > 0) return x;", options: { breakLine: true } },
    { text: "    log(x);  // unreachable", options: { color: "FCA5A5", breakLine: true } },
    { text: "}" },
  ], { x: RX + 0.6, y: 2.36, w: RW - 1.1, h: 1.2, fontFace: MF, fontSize: 11, color: C.ice, margin: 0, lineSpacingMultiple: 1.04 });
  s.addText("parsed into an AST ▼", { x: RX, y: 3.75, w: RW, h: 0.3, fontFace: BF, fontSize: 10.5, italic: true, color: C.muted, align: "center", margin: 0 });
  { const cxp = RX + RW / 2;
    // root
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: cxp - 0.95, y: 4.15, w: 1.9, h: 0.5, rectRadius: 0.25, fill: { color: C.blue } });
    s.addText("method risk()", { x: cxp - 0.95, y: 4.15, w: 1.9, h: 0.5, fontFace: MF, fontSize: 10.5, bold: true, color: C.light, align: "center", valign: "middle", margin: 0 });
    const kids = [["if (x>0)", C.ink, C.border], ["return x", C.ink, C.border], ["log(x) ⚠", C.red, C.red]];
    const kw = 1.6, total = kids.length * kw + (kids.length - 1) * 0.25, startx = cxp - total / 2;
    kids.forEach((k, i) => {
      const kx = startx + i * (kw + 0.25);
      s.addShape(p.shapes.LINE, { x: cxp, y: 4.65, w: (kx + kw / 2) - cxp, h: 0.45, line: { color: "CBD5E1", width: 1 } });
      s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: kx, y: 5.1, w: kw, h: 0.5, rectRadius: 0.06, fill: { color: C.light }, line: { color: k[2], width: 1.3 } });
      s.addText(k[0], { x: kx, y: 5.1, w: kw, h: 0.5, fontFace: MF, fontSize: 10, bold: true, color: k[1], align: "center", valign: "middle", margin: 0 });
    });
  }

  // ════════════════ SLIDE 10 — DEEP DIVE: ENTROPY / SECRETS ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Deep dive · Technique", C.red);
  title(s, "Entropy & Secrets — spotting hardcoded keys");
  leftCol(s, C.red,
    "Entropy measures how random a string is. Real secrets (keys, tokens) look random — high entropy — while normal text and config do not.",
    ["Hardcoded API keys & tokens", "Passwords & connection strings", "Private keys / certificates", "Known prefixes: AKIA, ghp_, sk-…"],
    "A single committed credential can compromise a whole system. Entropy + prefix rules catch them before they reach repo history.");
  dpanel(s);
  const meter = (s, y, code, frac, col, label) => {
    s.addText(code, { x: RX + 0.4, y, w: RW - 0.8, h: 0.34, fontFace: MF, fontSize: 11.5, color: C.ink, margin: 0 });
    const mw = RW - 2.0;
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: RX + 0.4, y: y + 0.4, w: mw, h: 0.26, rectRadius: 0.13, fill: { color: "E2E8F0" } });
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: RX + 0.4, y: y + 0.4, w: mw * frac, h: 0.26, rectRadius: 0.13, fill: { color: col } });
    s.addText(label, { x: RX + 0.4 + mw + 0.12, y: y + 0.34, w: 1.4, h: 0.38, fontFace: BF, fontSize: 10.5, bold: true, color: col, valign: "middle", margin: 0 });
  };
  s.addText("ENTROPY SCORE (0–8 bits)", { x: RX + 0.4, y: 2.3, w: RW - 0.8, h: 0.3, fontFace: BF, fontSize: 10.5, bold: true, color: C.muted, charSpacing: 1, margin: 0 });
  meter(s, 2.85, 'apiKey = "AKIAJ4F2K7Q7VZ8XN1"', 0.86, C.red, "4.9 ⚠");
  meter(s, 4.15, 'greeting = "hello world"', 0.34, C.green, "2.6 ✓");
  meter(s, 5.45, 'token = "ghp_xT9...";', 0.9, C.red, "5.1 ⚠");

  // ════════════════ SLIDE 11 — DEEP DIVE: TEMPORAL RISK ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Deep dive · Technique", C.amber);
  title(s, "Temporal Risk — learning from change history");
  leftCol(s, C.amber,
    "Temporal risk looks beyond this PR at a file's history — how often it changes, whether changes are getting riskier, and security regressions over time.",
    ["Churn hot-spots (frequently changed files)", "Escalating change patterns", "Security erosion across commits", "Files with repeated past incidents"],
    "Files that change constantly and trend riskier are where defects cluster — history tells you where to look first.");
  dpanel(s);
  s.addText("CHANGE FREQUENCY  ·  RISK TREND", { x: RX + 0.4, y: 2.3, w: RW - 0.8, h: 0.3, fontFace: BF, fontSize: 10.5, bold: true, color: C.muted, charSpacing: 1, margin: 0 });
  const hot = [
    ["PaymentService.java", 14, 0.95, C.red, "↑ degrading"],
    ["AccountDao.java", 9, 0.62, C.amber, "→ stable"],
    ["JsonWrapperUtils.java", 4, 0.3, C.green, "✓ improving"],
  ];
  hot.forEach(([name, n, frac, col, trend], i) => {
    const y = 2.85 + i * 1.05;
    s.addText(name, { x: RX + 0.4, y, w: RW - 0.8, h: 0.3, fontFace: MF, fontSize: 11, color: C.ink, margin: 0 });
    const mw = RW - 2.3;
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: RX + 0.4, y: y + 0.33, w: mw, h: 0.26, rectRadius: 0.05, fill: { color: "E2E8F0" } });
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: RX + 0.4, y: y + 0.33, w: mw * frac, h: 0.26, rectRadius: 0.05, fill: { color: col } });
    s.addText(`${n}×`, { x: RX + 0.4 + mw + 0.12, y: y + 0.27, w: 0.5, h: 0.38, fontFace: BF, fontSize: 11, bold: true, color: C.ink, valign: "middle", margin: 0 });
    s.addText(trend, { x: RX + 0.4 + mw - 1.4, y: y, w: 1.5, h: 0.3, fontFace: BF, fontSize: 9.5, bold: true, color: col, align: "right", margin: 0 });
  });

  // ════════════════ SLIDE 12 — DEEP DIVE: BLAST RADIUS ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Deep dive · Technique", C.teal);
  title(s, "Blast Radius — how far a change ripples");
  leftCol(s, C.teal,
    "CIAA walks the service-dependency graph (NetworkX / Neo4j) — or derives it from real signals — to find everything that transitively depends on what you changed.",
    ["Directly & transitively affected services", "Shared-library edits with wide fan-out", "Breaking changes that amplify reach", "A 0–100 reach score with a breakdown"],
    "Tells reviewers and release managers how risky a merge is, and which teams to involve — before it ships.");
  dpanel(s);
  { const sx = RX + 0.45, sy = 4.35;             // changed (source) node
    const depX = RX + 3.55, dys = [3.05, 3.95, 4.85, 5.6];
    const line2 = (x1, y1, x2, y2, col) => s.addShape(p.shapes.LINE, { x: Math.min(x1, x2), y: Math.min(y1, y2), w: Math.abs(x2 - x1), h: Math.abs(y2 - y1), line: { color: col, width: 1.25 }, flipH: x2 < x1, flipV: y2 < y1 });
    dys.forEach(dy => line2(sx + 1.75, sy + 0.3, depX, dy + 0.27, "CBD5E1"));
    // score pill
    s.addText("BLAST RADIUS", { x: RX + 0.4, y: 2.2, w: 2.4, h: 0.3, fontFace: BF, fontSize: 10.5, bold: true, color: C.muted, charSpacing: 1, margin: 0 });
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: RX + RW - 1.95, y: 2.18, w: 1.55, h: 0.46, rectRadius: 0.23, fill: { color: C.red } });
    s.addText("61 / 100 · HIGH", { x: RX + RW - 1.95, y: 2.18, w: 1.55, h: 0.46, fontFace: BF, fontSize: 10.5, bold: true, color: C.light, align: "center", valign: "middle", margin: 0 });
    // source node
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: sx, y: sy, w: 1.75, h: 0.62, rectRadius: 0.07, fill: { color: C.red } });
    s.addText("shared-domain", { x: sx, y: sy + 0.06, w: 1.75, h: 0.3, fontFace: MF, fontSize: 10.5, bold: true, color: C.light, align: "center", margin: 0 });
    s.addText("changed", { x: sx, y: sy + 0.34, w: 1.75, h: 0.24, fontFace: BF, fontSize: 9, italic: true, color: "FECACA", align: "center", margin: 0 });
    // dependent nodes
    const deps = [["payments-svc", C.amber], ["accounts-svc", C.teal], ["ledger-svc", C.amber], ["billing-svc", C.teal]];
    deps.forEach(([name, col], i) => {
      const dy = dys[i];
      s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: depX, y: dy, w: 1.85, h: 0.54, rectRadius: 0.07, fill: { color: C.light }, line: { color: col, width: 1.4 } });
      s.addText(name, { x: depX, y: dy, w: 1.85, h: 0.54, fontFace: MF, fontSize: 10, bold: true, color: C.ink, align: "center", valign: "middle", margin: 0 });
    });
    s.addText("transitively depends on the changed library", { x: RX + 0.4, y: 6.35, w: RW - 0.8, h: 0.3, fontFace: BF, fontSize: 9.5, italic: true, color: C.muted, align: "center", margin: 0 });
  }

  // ════════════════ SLIDE 13 — DEEP DIVE: SCA / CVE ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Deep dive · Technique", C.amber);
  title(s, "SCA — finding known CVEs in dependencies");
  leftCol(s, C.amber,
    "Software Composition Analysis inventories your third-party dependencies and checks each version against the OSV vulnerability database for known CVEs.",
    ["Known CVEs in declared dependencies", "Vulnerable version ranges (OSV.dev)", "Severity + fixed-version guidance", "Maven pom.xml & other manifests"],
    "Most of your code is dependencies you didn't write — SCA catches a known-exploited library before it reaches production.");
  dpanel(s);
  { // manifest box
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: RX + 0.4, y: 2.25, w: RW - 0.8, h: 0.95, rectRadius: 0.07, fill: { color: C.navy } });
    s.addText([
      { text: "pom.xml", options: { color: C.teal, bold: true, breakLine: true } },
      { text: "log4j-core · jackson-databind · commons-text …", options: { color: C.ice } },
    ], { x: RX + 0.6, y: 2.36, w: RW - 1.1, h: 0.75, fontFace: MF, fontSize: 10.5, margin: 0, lineSpacingMultiple: 1.1 });
    s.addText("▼  scanned against OSV.dev", { x: RX, y: 3.28, w: RW, h: 0.3, fontFace: BF, fontSize: 10, italic: true, color: C.muted, align: "center", margin: 0 });
    const cves = [
      ["log4j-core @ 2.14.1", "CVE-2021-44228 (Log4Shell)", "CRITICAL", C.red],
      ["jackson-databind @ 2.9.8", "CVE-2020-36518", "HIGH", C.amber],
      ["commons-text @ 1.9", "CVE-2022-42889 (Text4Shell)", "HIGH", C.amber],
    ];
    cves.forEach(([pkg, cve, sev, col], i) => {
      const y = 3.7 + i * 0.92;
      s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: RX + 0.4, y, w: RW - 0.8, h: 0.76, rectRadius: 0.07, fill: { color: C.light }, line: { color: C.border, width: 1 } });
      s.addShape(p.shapes.RECTANGLE, { x: RX + 0.4, y, w: 0.07, h: 0.76, fill: { color: col } });
      s.addText(pkg, { x: RX + 0.62, y: y + 0.1, w: RW - 2.2, h: 0.3, fontFace: MF, fontSize: 11, bold: true, color: C.ink, margin: 0 });
      s.addText(cve, { x: RX + 0.62, y: y + 0.42, w: RW - 2.2, h: 0.28, fontFace: BF, fontSize: 10, color: C.muted, margin: 0 });
      s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: RX + RW - 1.45, y: y + 0.22, w: 1.1, h: 0.34, rectRadius: 0.17, fill: { color: col } });
      s.addText(sev, { x: RX + RW - 1.45, y: y + 0.22, w: 1.1, h: 0.34, fontFace: BF, fontSize: 9.5, bold: true, color: C.light, align: "center", valign: "middle", margin: 0 });
    });
  }

  // ════════════════ SLIDE 14 — DETERMINISTIC GATE ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Governance", C.indigo);
  title(s, "A deterministic, auditable merge decision");
  const gates = [
    [C.green, I.check, "APPROVE", "Pipeline can proceed", ["No blocking security/CVE findings", "Tests cover new critical paths", "No breaking contract changes"]],
    [C.amber, I.warn, "HOLD", "Human review required", ["Coverage dropped on changed code", "Medium-severity findings", "New untested security-sensitive method"]],
    [C.red, I.x, "BLOCK", "Must fix before merge", ["Hardcoded secret / critical CVE", "Irreversible destructive migration", "Removed public API with consumers"]],
  ];
  const gw = (W - 2 * MX - 2 * 0.45) / 3;
  gates.forEach(([ac, ic, name, sub, list], i) => {
    const x = MX + i * (gw + 0.45);
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y: 1.95, w: gw, h: 3.7, rectRadius: 0.1, fill: { color: C.card }, line: { color: ac, width: 1.5 } });
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y: 1.95, w: gw, h: 0.95, rectRadius: 0.1, fill: { color: ac } });
    s.addShape(p.shapes.RECTANGLE, { x, y: 2.55, w: gw, h: 0.35, fill: { color: ac } });
    s.addImage({ data: ic, x: x + 0.35, y: 2.2, w: 0.45, h: 0.45 });
    s.addText(name, { x: x + 0.9, y: 2.13, w: gw - 1.0, h: 0.6, fontFace: HF, fontSize: 21, bold: true, color: C.light, valign: "middle", margin: 0 });
    s.addText(sub, { x: x + 0.3, y: 3.0, w: gw - 0.6, h: 0.35, fontFace: BF, fontSize: 12, italic: true, color: ac, margin: 0 });
    s.addText(list.map(t => ({ text: t, options: { bullet: { indent: 14 }, breakLine: true, color: C.ink } })),
      { x: x + 0.34, y: 3.45, w: gw - 0.62, h: 2.0, fontFace: BF, fontSize: 12, color: C.ink, margin: 0, lineSpacingMultiple: 1.12, paraSpaceAfter: 6 });
  });
  s.addText([
    { text: "Most-restrictive-wins policy", options: { bold: true, color: C.ink } },
    { text: "  ·  every decision logged with a request ID  ·  RBAC override  ·  mark findings ", options: { color: C.muted } },
    { text: "valid / false-positive", options: { bold: true, color: C.ink } },
    { text: " to train the gate over time.", options: { color: C.muted } },
  ], { x: MX, y: 6.05, w: W - 2 * MX, h: 0.6, fontFace: BF, fontSize: 12.5, align: "center", margin: 0 });

  // ════════════════ SLIDE 8 — VS SAST ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Differentiation", C.amber);
  title(s, "Beyond SonarQube & Veracode");
  const yes = { text: "●", options: { color: C.green, bold: true } };
  const no  = { text: "—", options: { color: C.red, bold: true } };
  const part = { text: "◐", options: { color: C.amber, bold: true } };
  const hdr = (t) => ({ text: t, options: { bold: true, color: C.light, fontFace: HF, fontSize: 14, fill: { color: C.navy }, align: "left", valign: "middle" } });
  const cellH = (t) => ({ text: t, options: { bold: true, color: C.light, fontFace: HF, fontSize: 14, fill: { color: C.navy }, align: "center", valign: "middle" } });
  const rows = [
    ["Code-pattern & security findings", "●", "●"],
    ["Diff-native — understands the actual change", "◐", "●"],
    ["Cross-repo blast radius & downstream consumers", "—", "●"],
    ["Contract & serialization impact (DTO / JSON)", "—", "●"],
    ["Test adequacy for the changed code", "—", "●"],
    ["Deterministic merge gate with evidence", "—", "●"],
    ["Reviewer feedback loop (learns false positives)", "—", "●"],
    ["On-prem / self-hosted LLM (Bitbucket Server)", "◐", "●"],
  ];
  const mark = (v) => v === "●" ? yes : v === "—" ? no : v === "◐" ? part : { text: "●", options: { color: C.muted, bold: true } };
  const table = [[hdr("Capability"), cellH("Traditional SAST"), cellH("CIAA")]];
  rows.forEach((r, i) => {
    const fill = i % 2 ? "EEF2F8" : "FFFFFF";
    table.push([
      { text: r[0], options: { color: C.ink, fontFace: BF, fontSize: 12.5, align: "left", valign: "middle", fill: { color: fill }, margin: [3, 6, 3, 8] } },
      { text: [mark(r[1])], options: { align: "center", valign: "middle", fill: { color: fill } } },
      { text: [mark(r[2])], options: { align: "center", valign: "middle", fill: { color: fill } } },
    ]);
  });
  s.addTable(table, { x: MX, y: 1.95, w: W - 2 * MX, colW: [7.2, 2.4, 2.4], rowH: 0.5, border: { type: "solid", pt: 1, color: C.border }, valign: "middle" });
  s.addText([
    { text: "●", options: { color: C.green, bold: true } }, { text: " full   ", options: { color: C.muted } },
    { text: "◐", options: { color: C.amber, bold: true } }, { text: " partial   ", options: { color: C.muted } },
    { text: "—", options: { color: C.red, bold: true } }, { text: " not covered", options: { color: C.muted } },
  ], { x: MX, y: 6.55, w: 8, h: 0.4, fontFace: BF, fontSize: 11, margin: 0 });

  // ════════════════ SLIDE 9 — ENTERPRISE GRADE ════════════════
  s = mkSlide(); s.background = { color: C.navy };
  eyebrow(s, "Enterprise-grade", C.teal);
  s.addText("Production-ready, on-premises, and trustworthy", { x: MX, y: 0.85, w: W - 2 * MX, h: 0.8, fontFace: HF, fontSize: 28, bold: true, color: C.light, margin: 0 });
  const bigstats = [["20+", "specialist agents"], ["538", "automated tests"], ["0", "temperature — deterministic"], ["5", "LLM providers supported"]];
  const bw = (W - 2 * MX - 3 * 0.35) / 4;
  bigstats.forEach(([n, l], i) => {
    const x = MX + i * (bw + 0.35);
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y: 1.85, w: bw, h: 1.5, rectRadius: 0.1, fill: { color: C.panel }, line: { color: "2A3D63", width: 1 } });
    s.addText(n, { x, y: 1.95, w: bw, h: 0.75, fontFace: HF, fontSize: 34, bold: true, color: C.teal, align: "center", margin: 0 });
    s.addText(l, { x: x + 0.1, y: 2.72, w: bw - 0.2, h: 0.5, fontFace: BF, fontSize: 11.5, color: C.ice, align: "center", margin: 0 });
  });
  const feats = [
    [I.lock, "Access & audit", "RBAC + API keys, structured audit log, HMAC-verified webhooks."],
    [I.bolt, "Resilience", "Admission control, LLM concurrency limiter, circuit breakers, sanitized errors."],
    [I.server, "Operations", "Health / readiness probes, rate limiting, Prometheus metrics, Docker."],
    [I.eye, "Quality assurance", "LLM-as-judge panel scores every agent; a golden-case quality gate fails CI on any detection regression."],
  ];
  const fw = (W - 2 * MX - 0.4) / 2;
  feats.forEach(([ic, h2, b], i) => {
    const col = i % 2, row = Math.floor(i / 2);
    const x = MX + col * (fw + 0.4), y = 3.75 + row * 1.45;
    iconChip(s, ic, x, y, 0.6, "1E2C4A");
    s.addText(h2, { x: x + 0.8, y: y - 0.04, w: fw - 0.9, h: 0.4, fontFace: HF, fontSize: 15, bold: true, color: C.light, margin: 0 });
    s.addText(b, { x: x + 0.8, y: y + 0.36, w: fw - 0.9, h: 0.7, fontFace: BF, fontSize: 12, color: "AAB7D4", margin: 0, lineSpacingMultiple: 1.04 });
  });

  // ════════════════ SLIDE 10 — TOOLS & TECHNOLOGIES ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Under the hood", C.indigo);
  title(s, "Tools & technologies — and what each does");
  const tech = [
    [I.code, "Backend & API", C.blue, [
      ["Python 3.13 · FastAPI", "async REST API & analysis pipeline"],
      ["Pydantic v2 + settings", "typed models & env-driven config"],
      ["Uvicorn / Gunicorn", "production ASGI app server"],
    ]],
    [I.robot, "AI & orchestration", C.teal, [
      ["Claude · OpenAI · Azure · Ollama", "pluggable LLMs via one client"],
      ["20+ agent prompts @ temp 0", "deterministic security & impact analysis"],
      ["LangGraph (optional)", "graph-based agent pipeline + tracing"],
    ]],
    [I.net, "Impact & dependency graph", C.amber, [
      ["NetworkX", "in-process blast-radius & reference graph"],
      ["Neo4j (optional)", "persistent, org-wide service graph"],
      ["git clone + ripgrep", "cross-repo call-site tracing"],
    ]],
    [I.shield, "Security & SCA", C.red, [
      ["OSV.dev API", "known-CVE lookup for dependencies"],
      ["tree-sitter AST · taint · entropy", "precise 6-language parsing & detection"],
      ["Maven pom.xml parser", "direct-dependency vulnerability scan"],
    ]],
    [I.sitemap, "Frontend & visualization", C.indigo, [
      ["React 19 + Vite", "reviewer dashboard & persona views"],
      ["D3.js", "layered / force call-graph rendering"],
    ]],
    [I.server, "Data, reliability & ops", C.green, [
      ["SQLite · Redis · ChromaDB", "report store · cache · semantic search"],
      ["tenacity · OpenTelemetry · Prometheus", "retries · tracing · metrics"],
      ["Docker · OpenShift · pytest", "containers · deploy · 538 tests"],
    ]],
  ];
  const tcw = (W - 2 * MX - 0.4) / 2, tch = 1.7, tgy = 0.18, tY = 1.72;
  tech.forEach(([ic, head, ac, rowsT], i) => {
    const col = i % 2, row = Math.floor(i / 2);
    const x = MX + col * (tcw + 0.4), y = tY + row * (tch + tgy);
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y, w: tcw, h: tch, rectRadius: 0.08, fill: { color: C.card }, line: { color: C.border, width: 1 } });
    s.addShape(p.shapes.RECTANGLE, { x, y, w: 0.07, h: tch, fill: { color: ac } });
    iconChip(s, ic, x + 0.22, y + 0.22, 0.5, ac);
    s.addText(head, { x: x + 0.85, y: y + 0.22, w: tcw - 1.0, h: 0.5, fontFace: HF, fontSize: 14.5, bold: true, color: C.ink, valign: "middle", margin: 0 });
    const body = [];
    rowsT.forEach(([t, u]) => {
      body.push({ text: t + "  ", options: { bold: true, color: C.ink } });
      body.push({ text: "— " + u, options: { color: C.muted, breakLine: true } });
    });
    s.addText(body, { x: x + 0.26, y: y + 0.78, w: tcw - 0.5, h: tch - 0.9, fontFace: BF, fontSize: 10.5, margin: 0, lineSpacingMultiple: 1.02, paraSpaceAfter: 4 });
  });

  // ════════════════ SLIDE — ROADMAP ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Roadmap", C.teal);
  title(s, "Where CIAA goes next");
  s.addText("Detection quality is production-grade today — the roadmap scales it to an organisation-wide platform.",
    { x: MX, y: 1.38, w: W - 2 * MX, h: 0.4, fontFace: BF, fontSize: 13, color: C.muted, margin: 0 });

  const phases = [
    [I.bolt, "1", "Scale-out", C.blue, [
      "Redis-backed job queue + autoscaling analysis workers",
      "PostgreSQL report store (replaces SQLite)",
      "Cluster-wide rate & LLM concurrency limits",
    ], "Many teams, many PRs, zero queue contention"],
    [I.lock, "2", "Enterprise access", C.indigo, [
      "SSO / OIDC login with team-scoped RBAC",
      "Vault / KMS secret management",
      "SARIF export + required PR status checks",
    ], "Enforced merge gate, org-grade security"],
    [I.search, "3", "Deeper detection", C.teal, [
      "Transitive SCA (full dependency tree CVEs)",
      "Compiler-index (LSIF/SCIP) cross-repo references",
      "Golden corpus grown from real reviewed PRs",
    ], "Higher recall with measured precision"],
    [I.gauge, "4", "Operate at scale", C.green, [
      "Policy-as-code gate thresholds (OPA), per team",
      "Grafana SLOs, alerting & cost dashboards",
      "Helm + canary deploys, load & chaos testing",
    ], "Run it like any tier-1 internal platform"],
  ];
  const rw = (W - 2 * MX - 3 * 0.35) / 4, rY = 2.0, rH = 4.35;
  phases.forEach(([ic, num, name, ac, items, outcome], i) => {
    const x = MX + i * (rw + 0.35);
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y: rY, w: rw, h: rH, rectRadius: 0.09, fill: { color: C.card }, line: { color: C.border, width: 1 } });
    s.addShape(p.shapes.RECTANGLE, { x, y: rY, w: rw, h: 0.1, fill: { color: ac } });
    iconChip(s, ic, x + 0.24, rY + 0.3, 0.56, ac);
    s.addText(`PHASE ${num}`, { x: x + 0.95, y: rY + 0.3, w: rw - 1.1, h: 0.26, fontFace: BF, fontSize: 9.5, bold: true, color: ac, charSpacing: 2, margin: 0 });
    s.addText(name, { x: x + 0.95, y: rY + 0.55, w: rw - 1.1, h: 0.36, fontFace: HF, fontSize: 15, bold: true, color: C.ink, margin: 0 });
    s.addText(items.map(t => ({ text: t, options: { bullet: { indent: 10 }, breakLine: true } })),
      { x: x + 0.26, y: rY + 1.12, w: rw - 0.5, h: 2.45, fontFace: BF, fontSize: 10.5, color: C.ink, margin: 0, lineSpacingMultiple: 1.04, paraSpaceAfter: 7 });
    s.addShape(p.shapes.LINE, { x: x + 0.26, y: rY + rH - 0.78, w: rw - 0.52, h: 0, line: { color: C.border, width: 1 } });
    s.addText(outcome, { x: x + 0.26, y: rY + rH - 0.68, w: rw - 0.5, h: 0.56, fontFace: BF, fontSize: 9.5, italic: true, color: ac, margin: 0, lineSpacingMultiple: 1.0 });
    if (i < phases.length - 1)
      s.addText("›", { x: x + rw - 0.02, y: rY + rH / 2 - 0.3, w: 0.4, h: 0.6, fontFace: HF, fontSize: 22, bold: true, color: "C9D4E8", align: "center", valign: "middle", margin: 0 });
  });
  s.addText([
    { text: "Already shipped: ", options: { bold: true, color: C.ink } },
    { text: "tree-sitter ASTs · cross-agent correlation & Top Issues · CI quality gate · function-context prompts · confidence-weighted gate · warm repo mirror · LLM concurrency control", options: { color: C.muted } },
  ], { x: MX, y: 6.55, w: W - 2 * MX, h: 0.45, fontFace: BF, fontSize: 10.5, align: "center", margin: 0 });

  // ════════════════ SLIDE — ROADMAP TASK LIST (TABLE) ════════════════
  s = mkSlide(); s.background = { color: C.light };
  eyebrow(s, "Roadmap · Task list", C.teal);
  title(s, "Planned tasks");
  const themeColor = {
    "Functional intelligence": C.indigo,
    "Scale":     C.blue,
    "Access":    C.red,
    "Workflow":  C.amber,
    "Detection": C.teal,
    "Operations": C.green,
  };
  const tasks = [
    ["1", "FSD tracking & code-vs-spec validation", "Link each PR to its Functional Specification Document; flag changes that contradict or miss the spec (builds on today's functional-doc upload)", "Functional intelligence"],
    ["2", "Functional impact analysis", "Combine FSD + code change + dependent repos to report which business functions are affected and where to regression-test", "Functional intelligence"],
    ["3", "Job queue + autoscaling workers", "Redis-backed analysis queue; API stays responsive under many concurrent PRs", "Scale"],
    ["4", "PostgreSQL report store", "Replace SQLite for multi-instance deployments, retention policies", "Scale"],
    ["5", "Cluster-wide rate & LLM limits", "Shared Redis counters so limits hold across replicas", "Scale"],
    ["6", "SSO / OIDC with team RBAC", "IdP login, team-scoped data, per-team gate thresholds", "Access"],
    ["7", "Vault / KMS secret management", "No provider keys on disk; automatic rotation", "Access"],
    ["8", "SARIF export + PR status checks", "Findings into code-scanning dashboards; gate enforced as a required check", "Workflow"],
    ["9", "Transitive SCA", "Full dependency-tree CVE scan (mvn dependency:tree / lockfiles)", "Detection"],
    ["10", "Compiler-index cross-repo refs", "LSIF/SCIP for exact call-site resolution beyond grep", "Detection"],
    ["11", "Golden corpus from real PRs", "Grow the CI quality gate with anonymised production diffs", "Detection"],
    ["12", "OPA gate policies · SLO dashboards · Helm/canary", "Policy-as-code per team, Grafana SLOs, progressive deploys", "Operations"],
  ];
  const hdrC = (t, align = "left") => ({ text: t, options: { bold: true, color: C.light, fontFace: HF, fontSize: 12.5, fill: { color: C.navy }, align, valign: "middle" } });
  const tbl = [[hdrC("#", "center"), hdrC("Roadmap task"), hdrC("What it delivers"), hdrC("Theme", "center")]];
  tasks.forEach((row, i) => {
    const fill = i % 2 ? "EEF2F8" : "FFFFFF";
    const tc = themeColor[row[3]] || C.muted;
    tbl.push([
      { text: row[0], options: { color: "94A3B8", bold: true, fontFace: BF, fontSize: 10.5, align: "center", valign: "middle", fill: { color: fill } } },
      { text: row[1], options: { color: C.ink, bold: true, fontFace: BF, fontSize: 10.5, align: "left", valign: "middle", fill: { color: fill }, margin: [2, 4, 2, 6] } },
      { text: row[2], options: { color: C.muted, fontFace: BF, fontSize: 9.5, align: "left", valign: "middle", fill: { color: fill }, margin: [2, 4, 2, 6] } },
      { text: row[3], options: { color: tc, bold: true, fontFace: BF, fontSize: 9, align: "center", valign: "middle", fill: { color: fill } } },
    ]);
  });
  s.addTable(tbl, { x: MX, y: 1.62, w: W - 2 * MX, colW: [0.5, 3.5, 6.03, 2.0],
                    rowH: [0.34, ...tasks.map(() => 0.405)],
                    border: { type: "solid", pt: 0.75, color: C.border }, valign: "middle" });

  // ════════════════ SLIDE 11 — CLOSING ════════════════
  s = mkSlide(); s.background = { color: C.navy };
  s.addShape(p.shapes.RECTANGLE, { x: 0, y: 0, w: W, h: 0.18, fill: { color: C.teal } });
  s.addText("Catch production issues before they merge", { x: MX, y: 2.25, w: W - 2 * MX, h: 1.1, fontFace: HF, fontSize: 36, bold: true, color: C.light, align: "center", margin: 0 });
  s.addText("CIAA turns every Pull Request into a deep, consistent, evidence-backed review —\ncombining real impact analysis with a deterministic merge gate, at PR scale.",
    { x: 1.5, y: 3.55, w: W - 3.0, h: 1.0, fontFace: BF, fontSize: 15, color: C.ice, align: "center", margin: 0, lineSpacingMultiple: 1.15 });
  const tags = ["Deep PR Review", "Real Impact Analysis", "Deterministic Governance"];
  let tcx = (W - (tags.reduce((a, t) => a + 0.5 + t.length * 0.11, 0) + (tags.length - 1) * 0.25)) / 2;
  tags.forEach((t) => {
    const wt = 0.5 + t.length * 0.11;
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: tcx, y: 4.95, w: wt, h: 0.5, rectRadius: 0.25, fill: { color: C.panel }, line: { color: C.teal, width: 1 } });
    s.addText(t, { x: tcx, y: 4.95, w: wt, h: 0.5, fontFace: BF, fontSize: 12.5, bold: true, color: C.teal, align: "center", valign: "middle", margin: 0 });
    tcx += wt + 0.25;
  });

  await p.writeFile({ fileName: "/Users/samba/Documents/CIAA/impact-analyzer-2/deck/CIAA_Overview.pptx" });
  console.log("WROTE deck/CIAA_Overview.pptx");
})();
