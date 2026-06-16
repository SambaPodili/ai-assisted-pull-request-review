const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const fa = require("react-icons/fa");

// Same palette / typography as the main CIAA deck
const C = {
  navy: "0E1A33", panel: "16243F", ink: "1E293B", muted: "64748B",
  light: "FFFFFF", card: "F6F8FB", border: "E2E8F0",
  teal: "2DD4BF", blue: "38BDF8", indigo: "6366F1",
  green: "16A34A", amber: "D97706", red: "DC2626", ice: "CADCFC",
};
const HF = "Georgia", BF = "Calibri", MF = "Consolas";
const W = 13.333, H = 7.5, MX = 0.65;

async function icon(Comp, color = "#FFFFFF", size = 256) {
  const svg = ReactDOMServer.renderToStaticMarkup(React.createElement(Comp, { color, size: String(size) }));
  const png = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + png.toString("base64");
}

(async () => {
  const I = {};
  for (const [k, comp] of Object.entries({
    play: fa.FaPlayCircle, branch: fa.FaCodeBranch, gauge: fa.FaTachometerAlt,
    target: fa.FaBullseye, file: fa.FaFileAlt, comment: fa.FaCommentDots,
    plug: fa.FaPlug, lock: fa.FaLock, users: fa.FaUsers, window: fa.FaWindowMaximize,
    server: fa.FaServer, key: fa.FaKey, rocket: fa.FaRocket, cog: fa.FaCog,
  })) I[k] = await icon(comp);

  const p = new pptxgen();
  p.defineLayout({ name: "W", width: W, height: H });
  p.layout = "W";
  p.author = "CIAA";
  p.title  = "CIAA — Demo & Portal Integration";

  const iconChip = (s, data, x, y, d = 0.6, fill = C.navy) => {
    s.addShape(p.shapes.OVAL, { x, y, w: d, h: d, fill: { color: fill } });
    s.addImage({ data, x: x + d * 0.27, y: y + d * 0.27, w: d * 0.46, h: d * 0.46 });
  };

  // ═══════════════════ SLIDE 1 — DEMO ═══════════════════
  let s = p.addSlide();
  s.background = { color: C.navy };
  s.addShape(p.shapes.RECTANGLE, { x: 0, y: 0, w: 0.18, h: H, fill: { color: C.teal } });
  // faint agent-dot motif
  for (let i = 0; i < 6; i++)
    s.addShape(p.shapes.OVAL, { x: 10.7 + (i % 3) * 0.6, y: 0.65 + Math.floor(i / 3) * 0.6, w: 0.15, h: 0.15, fill: { color: C.teal, transparency: 30 + i * 8 } });

  s.addText("LIVE DEMO", { x: MX, y: 1.55, w: 8, h: 0.32, fontFace: BF, fontSize: 13, bold: true, color: C.teal, charSpacing: 4, margin: 0 });
  s.addText("CIAA in action", { x: MX, y: 1.95, w: 11.5, h: 1.0, fontFace: HF, fontSize: 48, bold: true, color: C.light, margin: 0 });
  s.addText("A real pull request from Bitbucket, analysed end-to-end — live.",
    { x: MX, y: 3.05, w: 11, h: 0.5, fontFace: BF, fontSize: 16, color: "AAB7D4", margin: 0 });

  // What you'll see — 6 steps in 2 rows
  const steps = [
    [I.branch,  "Connect & pick a PR",      "Bitbucket Server repo, live pull request"],
    [I.gauge,   "20+ agents run in parallel", "Watch the live pipeline & timings"],
    [I.target,  "Gate + Top Issues",          "APPROVE / HOLD / BLOCK with ranked, corroborated findings"],
    [I.file,    "FSD validation",             "The change checked against the functional spec"],
    [I.plug,    "Impact & blast radius",      "Dependent repos, breaking changes, call graph"],
    [I.comment, "Post to PR",                 "The review lands back on the pull request"],
  ];
  const cw = (W - 2 * MX - 2 * 0.35) / 3, chh = 1.28;
  steps.forEach(([ic, h2, b], i) => {
    const x = MX + (i % 3) * (cw + 0.35), y = 3.85 + Math.floor(i / 3) * (chh + 0.25);
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y, w: cw, h: chh, rectRadius: 0.09, fill: { color: C.panel }, line: { color: "2A3D63", width: 1 } });
    iconChip(s, ic, x + 0.22, y + 0.22, 0.52, C.teal);
    s.addText(`${i + 1}.  ${h2}`, { x: x + 0.88, y: y + 0.18, w: cw - 1.0, h: 0.34, fontFace: HF, fontSize: 13.5, bold: true, color: C.light, margin: 0 });
    s.addText(b, { x: x + 0.88, y: y + 0.55, w: cw - 1.05, h: 0.62, fontFace: BF, fontSize: 10.5, color: C.ice, margin: 0, lineSpacingMultiple: 1.02 });
  });
  s.addText("Runs on our own infrastructure with our self-hosted LLM — no code leaves the bank.",
    { x: MX, y: 6.85, w: W - 2 * MX, h: 0.4, fontFace: BF, fontSize: 11.5, italic: true, color: C.teal, align: "center", margin: 0 });

  // ═══════════════ SLIDE 2 — PORTAL INTEGRATION & DEPLOYMENT ═══════════════
  s = p.addSlide();
  s.background = { color: C.light };
  s.slideNumber = { x: 12.5, y: 7.04, w: 0.7, h: 0.3, fontFace: BF, fontSize: 9, color: "94A3B8", align: "right" };
  s.addText("NEXT STEP · INTEGRATION", { x: MX, y: 0.4, w: 8, h: 0.3, fontFace: BF, fontSize: 12, bold: true, color: C.indigo, charSpacing: 3, margin: 0 });
  s.addText("Integrating CIAA into our existing portal", { x: MX, y: 0.72, w: W - 2 * MX, h: 0.7, fontFace: HF, fontSize: 28, bold: true, color: C.ink, margin: 0 });
  s.addText("The portal already authenticates users via LDAP — CIAA plugs in behind it. Two workstreams:",
    { x: MX, y: 1.42, w: W - 2 * MX, h: 0.36, fontFace: BF, fontSize: 13, color: C.muted, margin: 0 });

  const colW2 = (W - 2 * MX - 0.45) / 2;
  const column = (x, ic, head, ac, items) => {
    s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y: 1.95, w: colW2, h: 4.7, rectRadius: 0.1, fill: { color: C.card }, line: { color: C.border, width: 1 } });
    s.addShape(p.shapes.RECTANGLE, { x, y: 1.95, w: colW2, h: 0.1, fill: { color: ac } });
    iconChip(s, ic, x + 0.28, 2.22, 0.56, ac);
    s.addText(head, { x: x + 1.0, y: 2.22, w: colW2 - 1.2, h: 0.56, fontFace: HF, fontSize: 17, bold: true, color: C.ink, valign: "middle", margin: 0 });
    let yy = 3.0;
    items.forEach(([t, d], i) => {
      s.addText(`${i + 1}`, { x: x + 0.3, y: yy, w: 0.34, h: 0.34, fontFace: BF, fontSize: 11, bold: true, color: C.light, align: "center", valign: "middle", fill: { color: ac }, margin: 0 });
      s.addText(t, { x: x + 0.78, y: yy - 0.02, w: colW2 - 1.05, h: 0.32, fontFace: BF, fontSize: 12.5, bold: true, color: C.ink, margin: 0 });
      s.addText(d, { x: x + 0.78, y: yy + 0.28, w: colW2 - 1.05, h: 0.52, fontFace: BF, fontSize: 10.5, color: C.muted, margin: 0, lineSpacingMultiple: 1.02 });
      yy += 0.92;
    });
  };

  column(MX, I.lock, "Portal integration (LDAP SSO)", C.indigo, [
    ["Trust the portal's LDAP session",
     "Portal issues a signed token (JWT / header) after LDAP login; a CIAA auth adapter validates it — no second login, no separate user store"],
    ["Map LDAP groups → CIAA roles",
     "AD groups map onto CIAA's existing RBAC registry (developer / reviewer / admin) to control gate overrides and admin functions"],
    ["Embed the CIAA UI in the portal",
     "Serve the dashboard as a portal route or iframe with the SSO handoff; CORS is already configurable (CORS_ORIGINS)"],
    ["Portal ↔ CIAA REST API",
     "Portal triggers analyses and pulls report summaries via the documented REST API using a scoped service-account key"],
  ]);

  column(MX + colW2 + 0.45, I.rocket, "Deployment", C.teal, [
    ["Deploy alongside the portal",
     "Backend + UI behind the existing nginx / OpenShift — manifests, systemd units and nginx configs already ship in the repo"],
    ["Wire the environment",
     "Self-hosted LLM endpoint, Bitbucket Server credentials, repo mirror (REPOS_ROOT) and gate thresholds via .env / ConfigMap"],
    ["Secrets & hardening",
     "Tokens in the platform secret store (no keys on disk), API-key auth between portal and CIAA, audit log shipped to SIEM"],
    ["Pilot → rollout",
     "Start with one team's repos + webhook auto-run on PRs; expand org-wide once the gate earns trust"],
  ]);

  s.addText([
    { text: "Already in place: ", options: { bold: true, color: C.ink } },
    { text: "RBAC registry · configurable CORS · nginx + OpenShift deployment configs · REST API with API-key auth · HMAC webhooks — the integration builds on shipped foundations.", options: { color: C.muted } },
  ], { x: MX, y: 6.85, w: W - 2 * MX, h: 0.45, fontFace: BF, fontSize: 10.5, align: "center", margin: 0 });

  await p.writeFile({ fileName: "/Users/samba/Documents/CIAA/impact-analyzer-2/deck/CIAA_Demo_Integration.pptx" });
  console.log("WROTE deck/CIAA_Demo_Integration.pptx");
})();
