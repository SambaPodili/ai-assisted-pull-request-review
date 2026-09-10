// shared/report-view/src/types.ts
// -----------------------------------------------------------------------------
// Minimal, dependency-free subset of the report shape this package's
// rendering reads — see README.md for why this is deliberately decoupled
// from vscode-extension/src/apiClient.ts's fuller types.

export interface CorrelatedIssue {
  title: string;
  file_path: string;
  line: number;
  severity: string;
  confidence: string;
  score: number;
  agents: string[];
  categories: string[];
  descriptions: string[];
}

export interface CodeFix {
  title: string;
  file_path: string;
  category: string;
  severity: string;
  before: string;
  after: string;
  diff: string;
  explanation: string;
  confidence: string;
}

export interface QAScenario {
  id: string;
  title: string;
  type: string;
  priority: string;
  description: string;
  steps: string[];
  affected_files: string[];
  test_skeleton: string;
  test_skeleton_filename: string;
}

export interface MermaidDiagram {
  diagram_type: string;
  mermaid_source: string;
  confidence: string;
  note: string;
}

export interface PathReviewSummary {
  agents_excluded: string[];
  steering_applied: boolean;
}

/** One entry from a repo's .gto-ignore.json — same fingerprint formula
 * (see fingerprint()) must be used by every host reading/writing that file. */
export interface SuppressedEntry {
  fingerprint: string;
  file_path: string;
  line: number;
  title: string;
  reason: string;
  suppressed_at: string;
}

export interface ReportViewModel {
  gate_decision: string;
  risk?: { risk_score?: number; rationale?: string };
  remediation?: { code_fixes?: CodeFix[]; fix_suggestions?: string[]; diagrams?: MermaidDiagram[] };
  qa_scenarios?: { scenarios?: QAScenario[] };
  path_review_summary?: PathReviewSummary | null;
  files_changed: number;
  files_changed_list: string[];
  top_issues: CorrelatedIssue[];
  duration_s: number;
  errors: string[];
}

export interface ReportViewOpts {
  suppressed: SuppressedEntry[];
  newFingerprints: Set<string>;
}
