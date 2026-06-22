const pptxgen = require("pptxgenjs");
const p = new pptxgen();
p.layout = "LAYOUT_WIDE";            // 13.3 x 7.5
p.author = "UOB";
p.title = "Code Assistant & Jira Workflow — Update";

const NAVY = "122C6E", NAVY2 = "1B3A8B", RED = "E4002B";
const ICE = "CADCFC", INK = "1F2937", MUT = "5B6675";
const CARD = "F2F5FB", WHITE = "FFFFFF", GREEN = "1E7D46";
const HF = "Georgia", BF = "Calibri";
const W = 13.3;

// UOB tally mark (red bars + strike) as shapes — x,y top-left, s = scale (inches per 24 units)
function uob(slide, x, y, s) {
  const r = (bx, by, bw, bh) => slide.addShape(p.shapes.ROUNDED_RECTANGLE,
    { x: x + bx * s, y: y + by * s, w: bw * s, h: bh * s, fill: { color: RED }, line: { type: "none" }, rectRadius: 0.02 });
  r(2.6, 5, 2.2, 14); r(7, 3, 2.2, 16); r(11.4, 5, 2.2, 14); r(15.8, 3.5, 2.2, 15.5); r(1.6, 8.4, 17.6, 2.2);
}

/* ---------- Slide 1 — Title ---------- */
let s = p.addSlide();
s.background = { color: NAVY };
uob(s, 0.9, 0.7, 0.05);
s.addText("UOB  ·  Group Technology & Operations", { x: 1.5, y: 0.72, w: 9, h: 0.4, fontFace: BF, fontSize: 13, color: ICE, charSpacing: 2 });
s.addText("Tooling Update", { x: 0.9, y: 2.4, w: 11.5, h: 1.1, fontFace: HF, fontSize: 44, bold: true, color: WHITE });
s.addText("Code Analysis & Review   ·   Code Assistant   ·   Jira Workflow (OSM)", { x: 0.92, y: 3.5, w: 12, h: 0.6, fontFace: BF, fontSize: 20, italic: true, color: ICE });
s.addShape(p.shapes.RECTANGLE, { x: 0.95, y: 4.4, w: 0.55, h: 0.07, fill: { color: RED }, line: { type: "none" } });

/* ---------- Slide 2 — Problem statement (simple) ---------- */
s = p.addSlide();
s.background = { color: WHITE };
s.addText("CODE ANALYSIS & REVIEW", { x: 0.72, y: 0.5, w: 12, h: 0.35, fontFace: BF, fontSize: 13, bold: true, color: RED, charSpacing: 2 });
s.addText("The problem we're solving", { x: 0.7, y: 0.85, w: 12, h: 0.8, fontFace: HF, fontSize: 34, bold: true, color: NAVY });
s.addText("In plain terms — why Code Analysis & Review matters for every team.",
  { x: 0.72, y: 1.65, w: 12, h: 0.45, fontFace: BF, fontSize: 16, italic: true, color: MUT });

// Left card — Today (the pain)
s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: 0.7, y: 2.35, w: 5.9, h: 4.55, fill: { color: CARD }, line: { color: "E1E7F2", width: 1 }, rectRadius: 0.12 });
s.addText("Today", { x: 1.0, y: 2.6, w: 5.3, h: 0.5, fontFace: BF, fontSize: 18, bold: true, color: RED });
s.addText([
  { text: "Code reviews are manual, slow and inconsistent.", options: { bullet: true, breakLine: true } },
  { text: "Bugs, security gaps and breaking changes can slip through.", options: { bullet: true, breakLine: true } },
  { text: "Reviewers spend hours per pull request.", options: { bullet: true, breakLine: true } },
  { text: "Quality depends on who happens to review.", options: { bullet: true, breakLine: true } },
  { text: "New developers wait on busy reviewers for feedback.", options: { bullet: true } },
], { x: 1.0, y: 3.25, w: 5.3, h: 3.45, fontFace: BF, fontSize: 16, color: INK, lineSpacingMultiple: 1.25, paraSpaceAfter: 6 });

// Right card — With Code Analysis & Review (the fix)
s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: 6.8, y: 2.35, w: 5.8, h: 4.55, fill: { color: NAVY }, line: { type: "none" }, rectRadius: 0.12 });
s.addText("With Code Analysis & Review", { x: 7.1, y: 2.6, w: 5.3, h: 0.5, fontFace: BF, fontSize: 18, bold: true, color: ICE });
s.addText([
  { text: "Every change is reviewed automatically, in minutes.", options: { bullet: true, breakLine: true } },
  { text: "Flags security, breaking changes, schema/data risk & test gaps.", options: { bullet: true, breakLine: true } },
  { text: "One clear decision: Approve / Hold / Block.", options: { bullet: true, breakLine: true } },
  { text: "Consistent for everyone — not reviewer-dependent.", options: { bullet: true, breakLine: true } },
  { text: "Faster feedback, so teams ship faster and safer.", options: { bullet: true } },
], { x: 7.1, y: 3.25, w: 5.2, h: 3.45, fontFace: BF, fontSize: 16, color: WHITE, lineSpacingMultiple: 1.25, paraSpaceAfter: 6 });

/* ---------- Slide 3 — Status & adoption ---------- */
s = p.addSlide();
s.background = { color: WHITE };
s.addText("Where we are & what's next", { x: 0.7, y: 0.55, w: 12, h: 0.8, fontFace: HF, fontSize: 34, bold: true, color: NAVY });

// Card A — Jira Workflow Adoption
s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: 0.7, y: 1.7, w: 5.9, h: 4.9, fill: { color: WHITE }, line: { color: "D8E0F0", width: 1.5 }, rectRadius: 0.12 });
s.addShape(p.shapes.RECTANGLE, { x: 0.7, y: 1.7, w: 0.13, h: 4.9, fill: { color: NAVY2 }, line: { type: "none" } });
s.addText("Jira Workflow Adoption — OSM", { x: 1.05, y: 1.95, w: 5.3, h: 0.6, fontFace: BF, fontSize: 19, bold: true, color: NAVY });
s.addText([{ text: "Aldon-free deployment", options: { bullet: true } }],
  { x: 1.05, y: 2.7, w: 5.3, h: 0.5, fontFace: BF, fontSize: 16, color: INK });
s.addText("Status", { x: 1.05, y: 3.4, w: 5, h: 0.35, fontFace: BF, fontSize: 13, bold: true, color: MUT, charSpacing: 1 });
const pill = (x, label, done) => {
  s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y: 3.8, w: 1.45, h: 0.55, fill: { color: done ? "E4F2E8" : "FFF3D6" }, line: { color: done ? GREEN : "E0A800", width: 1 }, rectRadius: 0.08 });
  s.addText((done ? "✓ " : "") + label, { x, y: 3.8, w: 1.45, h: 0.55, align: "center", valign: "middle", fontFace: BF, fontSize: 13, bold: true, color: done ? GREEN : "8A6A00" });
};
pill(1.05, "SIT", true); pill(2.65, "UAT", true); pill(4.25, "PROD", false);
s.addText("PROD — planning for implementation.", { x: 1.05, y: 4.55, w: 5.3, h: 0.45, fontFace: BF, fontSize: 14, italic: true, color: MUT });
s.addText([
  { text: "FDS application", options: { bold: true, bullet: true } },
  { text: " — onboarding starts by end of July." },
], { x: 1.05, y: 5.3, w: 5.3, h: 0.9, fontFace: BF, fontSize: 15, color: INK, lineSpacingMultiple: 1.2 });

// Card B — Code Assistant adoption & enablement
s.addShape(p.shapes.ROUNDED_RECTANGLE, { x: 6.8, y: 1.7, w: 5.8, h: 4.9, fill: { color: WHITE }, line: { color: "D8E0F0", width: 1.5 }, rectRadius: 0.12 });
s.addShape(p.shapes.RECTANGLE, { x: 6.8, y: 1.7, w: 0.13, h: 4.9, fill: { color: RED }, line: { type: "none" } });
s.addText("Code Assistant — Adoption & Enablement", { x: 7.15, y: 1.95, w: 5.2, h: 0.6, fontFace: BF, fontSize: 19, bold: true, color: NAVY });
s.addText([
  { text: "Model upgraded to Llama Maverick — effort gains will increase.", options: { bullet: true, breakLine: true } },
  { text: "Reducing initial setup so users can start using it right away.", options: { bullet: true, breakLine: true } },
  { text: "Continuing to explore new models.", options: { bullet: true } },
], { x: 7.15, y: 2.8, w: 5.2, h: 3.4, fontFace: BF, fontSize: 16, color: INK, lineSpacingMultiple: 1.3, paraSpaceAfter: 10 });

p.writeFile({ fileName: "deck/Code_Assistant_Update.pptx" }).then(f => console.log("WROTE", f));
