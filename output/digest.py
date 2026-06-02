"""
output/digest.py
-----------------
Daily email digest for the CIAA Impact Analyzer.

Summarises the review queue and API spend over the last 24 hours (configurable)
and emails it to a list of recipients via SMTP.

Configuration (config/settings.py):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_USE_TLS, SMTP_FROM
  DIGEST_RECIPIENTS  — comma-separated email addresses
  DIGEST_ENABLED     — must be true for the scheduler to fire

The digest body is plain HTML (renders in every mail client without CSS files).
"""
from __future__ import annotations
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


# ── Data assembly ─────────────────────────────────────────────────────────────

def build_digest_data(days: int = 1) -> dict:
    """
    Gather the numbers for the digest by reusing the insights endpoints'
    underlying functions (single source of truth — no duplicated logic).
    """
    from api.routes.insights import pr_priority_queue, api_cost

    queue_data = pr_priority_queue(limit=200, days=days)
    cost_data  = api_cost(weeks=max(1, (days + 6) // 7))

    queue = queue_data["queue"]
    blocks   = [q for q in queue if q["gate"] == "BLOCK"]
    holds    = [q for q in queue if q["gate"] == "HOLD"]
    approves = [q for q in queue if q["gate"] == "APPROVE"]

    return {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "window_days":  days,
        "total":        len(queue),
        "blocks":       blocks,
        "holds":        holds,
        "approve_count": len(approves),
        "cost_usd":     cost_data["summary"]["total_cost_usd"],
        "total_tokens": cost_data["summary"]["total_tokens"],
        "fallbacks":    cost_data["summary"]["fallback_count"],
        "top_prs":      sorted(queue, key=lambda x: -x["risk_score"])[:5],
    }


# ── HTML rendering ────────────────────────────────────────────────────────────

def render_digest_html(d: dict) -> str:
    def _gate_chip(g):
        c = {"BLOCK": "#dc2626", "HOLD": "#f59e0b", "APPROVE": "#059669"}.get(g, "#7a8494")
        return (f'<span style="background:{c}1a;color:{c};border:1px solid {c}55;'
                f'border-radius:4px;padding:1px 7px;font-size:12px;font-weight:700">{g}</span>')

    def _pr_rows(prs):
        if not prs:
            return '<tr><td colspan="4" style="padding:8px;color:#7a8494;font-size:13px">None</td></tr>'
        out = []
        for p in prs:
            title = (p.get("pr_title") or p.get("source_ref") or "(untitled)")
            out.append(
                f'<tr style="border-top:1px solid #eee">'
                f'<td style="padding:7px 8px;font-size:13px"><b>{p["risk_score"]}</b></td>'
                f'<td style="padding:7px 8px">{_gate_chip(p["gate"])}</td>'
                f'<td style="padding:7px 8px;font-size:13px">{_esc(title)}</td>'
                f'<td style="padding:7px 8px;font-size:12px;color:#7a8494">{_esc(p["repo"])}'
                f'{(" · " + _esc(p["author"])) if p.get("author") else ""}</td>'
                f'</tr>'
            )
        return "".join(out)

    blocks_count = len(d["blocks"])
    holds_count  = len(d["holds"])

    return f"""\
<!DOCTYPE html><html><body style="margin:0;background:#f4f6f8;font-family:-apple-system,Segoe UI,sans-serif">
<div style="max-width:640px;margin:0 auto;padding:24px">
  <div style="background:#0d1117;border-radius:12px 12px 0 0;padding:20px 24px">
    <div style="color:#fff;font-size:20px;font-weight:700">📊 CIAA Daily Digest</div>
    <div style="color:#9fadbf;font-size:12px;margin-top:2px">{d['generated_at']} · last {d['window_days']} day(s)</div>
  </div>
  <div style="background:#fff;padding:22px 24px;border-radius:0 0 12px 12px;border:1px solid #e8eaed;border-top:none">

    <table width="100%" cellspacing="0" style="margin-bottom:20px">
      <tr>
        <td style="text-align:center;padding:10px;background:#fff1f2;border-radius:8px">
          <div style="font-size:26px;font-weight:800;color:#dc2626">{blocks_count}</div>
          <div style="font-size:11px;color:#991b1b">🚫 Blocked</div>
        </td>
        <td style="width:8px"></td>
        <td style="text-align:center;padding:10px;background:#fffbeb;border-radius:8px">
          <div style="font-size:26px;font-weight:800;color:#f59e0b">{holds_count}</div>
          <div style="font-size:11px;color:#92400e">⚠️ Needs review</div>
        </td>
        <td style="width:8px"></td>
        <td style="text-align:center;padding:10px;background:#f0fdf4;border-radius:8px">
          <div style="font-size:26px;font-weight:800;color:#059669">{d['approve_count']}</div>
          <div style="font-size:11px;color:#166534">✅ Approved</div>
        </td>
      </tr>
    </table>

    <div style="font-size:14px;font-weight:700;color:#0d1117;margin-bottom:8px">Highest-risk PRs</div>
    <table width="100%" cellspacing="0" style="border-collapse:collapse;margin-bottom:20px">
      <tr style="color:#9fadbf;font-size:10px;text-transform:uppercase">
        <td style="padding:4px 8px">Risk</td><td style="padding:4px 8px">Gate</td>
        <td style="padding:4px 8px">PR</td><td style="padding:4px 8px">Repo</td>
      </tr>
      {_pr_rows(d['top_prs'])}
    </table>

    <div style="background:#f7f8fa;border-radius:8px;padding:14px 16px;font-size:13px;color:#5a6a7e">
      💰 <b>API spend:</b> ${d['cost_usd']:.2f} · {d['total_tokens']:,} tokens
      {(' · ⚠️ ' + str(d['fallbacks']) + ' fallback(s)') if d['fallbacks'] else ''}
    </div>

    <div style="margin-top:18px;font-size:11px;color:#9fadbf;text-align:center">
      Generated by CIAA Impact Analyzer · This is an automated digest.
    </div>
  </div>
</div>
</body></html>"""


def render_digest_text(d: dict) -> str:
    """Plain-text fallback for mail clients that block HTML."""
    lines = [
        f"CIAA Daily Digest — {d['generated_at']} (last {d['window_days']} day(s))",
        "",
        f"  Blocked:      {len(d['blocks'])}",
        f"  Needs review: {len(d['holds'])}",
        f"  Approved:     {d['approve_count']}",
        f"  API spend:    ${d['cost_usd']:.2f} ({d['total_tokens']:,} tokens)",
        "",
        "Highest-risk PRs:",
    ]
    for p in d["top_prs"]:
        title = p.get("pr_title") or p.get("source_ref") or "(untitled)"
        lines.append(f"  [{p['gate']}] risk {p['risk_score']} — {title} ({p['repo']})")
    if not d["top_prs"]:
        lines.append("  None")
    return "\n".join(lines)


def _esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ── Sending ───────────────────────────────────────────────────────────────────

def send_digest(settings=None, days: int = 1) -> dict:
    """
    Build and send the digest. Returns a status dict.
    Raises nothing — always returns a structured result so callers/schedulers
    don't crash on SMTP errors.
    """
    from config.settings import get_settings
    cfg = settings or get_settings()

    recipients = [e.strip() for e in (cfg.digest_recipients or "").split(",") if e.strip()]
    if not cfg.smtp_host:
        return {"ok": False, "reason": "SMTP_HOST not configured"}
    if not recipients:
        return {"ok": False, "reason": "DIGEST_RECIPIENTS not configured"}

    data = build_digest_data(days=days)
    html = render_digest_html(data)
    text = render_digest_text(data)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (f"CIAA Digest — {len(data['blocks'])} blocked, "
                      f"{len(data['holds'])} need review")
    msg["From"]    = cfg.smtp_from or cfg.smtp_user or "ciaa@localhost"
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as server:
            if cfg.smtp_use_tls:
                server.starttls()
            if cfg.smtp_user and cfg.smtp_password:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.sendmail(msg["From"], recipients, msg.as_string())
        log.info("Digest sent to %d recipient(s)", len(recipients))
        return {"ok": True, "recipients": recipients,
                "blocked": len(data["blocks"]), "needs_review": len(data["holds"])}
    except Exception as exc:
        log.error("Digest send failed: %s", exc)
        return {"ok": False, "reason": str(exc)}
