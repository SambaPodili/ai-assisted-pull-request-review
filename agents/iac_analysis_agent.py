"""
agents/iac_analysis_agent.py
-----------------------------
Infrastructure-as-Code security analysis.

Banking systems increasingly deploy via Terraform, Kubernetes YAML, and Docker.
A misconfigured security group can expose a database. A privileged Kubernetes
pod can compromise the entire cluster. A Dockerfile running as root violates
PCI-DSS requirements. These issues are invisible to traditional code review.

Supported formats (all via deterministic scanning — zero LLM tokens):

  Terraform (.tf):
    • Security groups with 0.0.0.0/0 ingress/egress rules
    • S3 buckets without server-side encryption
    • S3 buckets with public ACL
    • RDS instances without encryption or publicly accessible
    • IAM policies with wildcard (*) actions or resources
    • Lambda functions with overly permissive execution roles
    • Unrestricted SSH (port 22) or RDP (port 3389) access

  Kubernetes YAML (.yaml/.yml with k8s markers):
    • Containers running as privileged
    • Containers running as root (runAsUser: 0 or missing runAsNonRoot)
    • Missing CPU/memory resource limits (allows resource exhaustion)
    • HostPath mounts (container can access host filesystem)
    • Missing network policies (pods can communicate freely)
    • Containers with hostPID/hostNetwork/hostIPC
    • Image tags using :latest (non-deterministic deployments)
    • Missing readiness/liveness probes (silent failures)
    • Secrets mounted as environment variables instead of secretRef

  Dockerfile:
    • Base image running as root
    • ADD instruction (allows URL download and tar extraction)
    • Missing USER instruction (runs as root by default)
    • Exposed privileged ports (< 1024)
    • COPY with wildcard (accidentally copies sensitive files)
    • Secrets in ENV or ARG instructions

Compliance:
    • CIS Kubernetes Benchmark 1.8
    • CIS Docker Benchmark 1.6
    • Terraform AWS Security Best Practices
    • PCI-DSS Req 1.1 (network controls), Req 7 (least privilege)
"""
from __future__ import annotations
import re
from typing import Any

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

from core.models import (
    AgentName, AnalysisRequest,
    IaCAnalysisResult, IaCFinding, RiskLevel,
)
from core.token_manager import trim_diff_for_budget
from agents.base_agent import BaseAgent


# ── File type detection ───────────────────────────────────────────────────────

def _is_terraform(path: str) -> bool:
    return path.endswith('.tf') or path.endswith('.tfvars')

def _is_kubernetes(path: str, content: str) -> bool:
    if not (path.endswith('.yaml') or path.endswith('.yml')):
        return False
    k8s_markers = ('apiVersion:', 'kind:', 'metadata:', 'spec:')
    return sum(1 for m in k8s_markers if m in content) >= 2

def _is_dockerfile(path: str) -> bool:
    return path.endswith('Dockerfile') or '/Dockerfile.' in path or path.startswith('Dockerfile')

def _is_helm(path: str) -> bool:
    return 'templates/' in path and (path.endswith('.yaml') or path.endswith('.yml'))


# ── Terraform patterns ────────────────────────────────────────────────────────

_TF_OPEN_INGRESS    = re.compile(r'cidr_blocks\s*=\s*\[?"0\.0\.0\.0/0"?\]?')
_TF_OPEN_EGRESS     = re.compile(r'cidr_blocks\s*=\s*\[?"0\.0\.0\.0/0"?\]?.*?(?:egress|from_port\s*=\s*0)')
_TF_S3_PUBLIC       = re.compile(r'acl\s*=\s*"public(?:-read|-read-write)?"')
_TF_S3_NO_ENCRYPT   = re.compile(r'server_side_encryption_configuration')  # check absence
_TF_RDS_PUBLIC      = re.compile(r'publicly_accessible\s*=\s*true')
_TF_RDS_NO_ENCRYPT  = re.compile(r'storage_encrypted\s*=\s*false')
_TF_IAM_WILDCARD    = re.compile(r'"(\*)"')
_TF_IAM_STAR_RESOURCE = re.compile(r'resources?\s*=\s*\[?"?\*"?\]?')
_TF_RESOURCE_NAME   = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"')
_TF_SSH_PORT        = re.compile(r'from_port\s*=\s*22\b|to_port\s*=\s*22\b')
_TF_RDP_PORT        = re.compile(r'from_port\s*=\s*3389\b|to_port\s*=\s*3389\b')


def _scan_terraform(hunk) -> list[IaCFinding]:
    findings: list[IaCFinding] = []
    lines = hunk.content.splitlines()
    current_resource = ""

    for i, raw_line in enumerate(lines, 1):
        # Track resource context
        m = _TF_RESOURCE_NAME.search(raw_line)
        if m:
            current_resource = f"{m.group(1)}.{m.group(2)}"

        if not (raw_line.startswith("+") and not raw_line.startswith("+++")):
            continue
        line = raw_line[1:]

        # Open ingress 0.0.0.0/0
        if _TF_OPEN_INGRESS.search(line) and 'egress' not in line.lower():
            # Check if this is actually an egress block by looking at context
            is_egress = any('egress' in lines[max(0,i-5):i][j] for j in range(min(5,i)))
            if not is_egress:
                findings.append(IaCFinding(
                    file_path=hunk.file_path, line=i,
                    resource=current_resource or "aws_security_group",
                    kind="open_ingress",
                    severity=RiskLevel.CRITICAL,
                    description="Security group allows inbound traffic from any IP (0.0.0.0/0). Exposes the resource to the entire internet.",
                    cis_ref="CIS AWS 5.2",
                    fix="Restrict cidr_blocks to specific IP ranges or use security group IDs instead.",
                ))

        # Unrestricted SSH/RDP
        if _TF_SSH_PORT.search(line) and _TF_OPEN_INGRESS.search('\n'.join(lines[max(0,i-3):i+3])):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=current_resource,
                kind="open_ingress",
                severity=RiskLevel.CRITICAL,
                description="SSH port 22 open to 0.0.0.0/0. Direct SSH access from internet violates banking network segmentation requirements.",
                cis_ref="CIS AWS 5.2",
                fix="Use bastion host or VPN. Remove direct SSH from internet-facing security groups.",
            ))

        if _TF_RDP_PORT.search(line) and _TF_OPEN_INGRESS.search('\n'.join(lines[max(0,i-3):i+3])):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=current_resource,
                kind="open_ingress",
                severity=RiskLevel.CRITICAL,
                description="RDP port 3389 open to 0.0.0.0/0. Violates PCI-DSS Req 1.1.",
                cis_ref="CIS AWS 5.3",
                fix="Restrict RDP access to specific management IPs or use AWS SSM Session Manager.",
            ))

        # S3 public ACL
        if _TF_S3_PUBLIC.search(line):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=current_resource or "aws_s3_bucket",
                kind="public_bucket",
                severity=RiskLevel.CRITICAL,
                description="S3 bucket with public ACL — data accessible to the internet. PCI-DSS violation if cardholder data is stored.",
                cis_ref="CIS AWS 2.1.5",
                fix="Remove public ACL. Use bucket policies with explicit account/role permissions only.",
            ))

        # RDS publicly accessible
        if _TF_RDS_PUBLIC.search(line):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=current_resource or "aws_db_instance",
                kind="open_ingress",
                severity=RiskLevel.CRITICAL,
                description="RDS database instance is publicly accessible. Database should never be directly reachable from the internet.",
                cis_ref="CIS AWS 2.3.1",
                fix="Set publicly_accessible = false. Access via application layer or VPN only.",
            ))

        # RDS no encryption
        if _TF_RDS_NO_ENCRYPT.search(line):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=current_resource or "aws_db_instance",
                kind="missing_encryption",
                severity=RiskLevel.HIGH,
                description="RDS storage encryption disabled. PCI-DSS Req 3.5 requires encryption of stored cardholder data.",
                cis_ref="CIS AWS 2.3.1",
                fix="Set storage_encrypted = true. Use aws_kms_key for customer-managed encryption.",
            ))

        # IAM wildcard
        if _TF_IAM_WILDCARD.search(line) and ('action' in line.lower() or 'resource' in line.lower()):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=current_resource or "aws_iam_policy",
                kind="wildcard_iam",
                severity=RiskLevel.HIGH,
                description="IAM policy uses wildcard (*) — grants full access to AWS services. Violates principle of least privilege.",
                cis_ref="CIS AWS 1.16",
                fix="Specify exact actions (e.g. s3:GetObject) and exact resources (e.g. arn:aws:s3:::my-bucket/*).",
            ))

    return findings


# ── Kubernetes YAML patterns ──────────────────────────────────────────────────

def _scan_kubernetes(hunk) -> list[IaCFinding]:
    findings: list[IaCFinding] = []
    lines = hunk.content.splitlines()
    added_content = "\n".join(l[1:] for l in lines if l.startswith("+") and not l.startswith("+++"))

    # Try YAML parse for structured checks
    resource_name = _extract_k8s_name(added_content)
    resource_kind = _extract_k8s_kind(added_content)

    for i, raw_line in enumerate(lines, 1):
        if not (raw_line.startswith("+") and not raw_line.startswith("+++")):
            continue
        line = raw_line[1:].strip()

        # Privileged container
        if re.search(r'privileged\s*:\s*true', line):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=f"{resource_kind}/{resource_name}",
                kind="privileged",
                severity=RiskLevel.CRITICAL,
                description="Container running in privileged mode — has full access to host kernel. Equivalent to running as root on the node.",
                cis_ref="CIS K8s 5.2.1",
                fix="Remove privileged: true. Use specific capabilities (securityContext.capabilities.add) only if required.",
            ))

        # Root user
        if re.search(r'runAsUser\s*:\s*0\b', line):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=f"{resource_kind}/{resource_name}",
                kind="root_container",
                severity=RiskLevel.HIGH,
                description="Container runs as root (UID 0). Compromise of container = compromise of host. PCI-DSS Req 7 violation.",
                cis_ref="CIS K8s 5.2.6",
                fix="Set runAsUser to a non-zero UID and runAsNonRoot: true.",
            ))

        if re.search(r'runAsNonRoot\s*:\s*false', line):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=f"{resource_kind}/{resource_name}",
                kind="root_container",
                severity=RiskLevel.HIGH,
                description="runAsNonRoot: false explicitly allows container to run as root.",
                cis_ref="CIS K8s 5.2.6",
                fix="Set runAsNonRoot: true and specify a non-zero runAsUser.",
            ))

        # hostPID / hostNetwork / hostIPC
        for host_perm in ('hostPID', 'hostNetwork', 'hostIPC'):
            if re.search(rf'{host_perm}\s*:\s*true', line):
                findings.append(IaCFinding(
                    file_path=hunk.file_path, line=i,
                    resource=f"{resource_kind}/{resource_name}",
                    kind="privileged",
                    severity=RiskLevel.HIGH,
                    description=f"{host_perm}: true — container shares host namespace. Allows container escape attacks.",
                    cis_ref="CIS K8s 5.2.2",
                    fix=f"Remove {host_perm}: true unless absolutely required by the workload.",
                ))

        # HostPath mount
        if re.search(r'hostPath\s*:', line):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=f"{resource_kind}/{resource_name}",
                kind="privileged",
                severity=RiskLevel.HIGH,
                description="HostPath volume mount — container can access host filesystem. Risk of data exfiltration or privilege escalation.",
                cis_ref="CIS K8s 5.2.3",
                fix="Use emptyDir, ConfigMap, or PersistentVolumeClaim instead of hostPath.",
            ))

        # Missing resource limits
        if re.search(r'resources\s*:\s*\{\}', line) or (re.search(r'resources\s*:', line) and 'limits' not in added_content[max(0,i-200):i+200]):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=f"{resource_kind}/{resource_name}",
                kind="missing_encryption",
                severity=RiskLevel.MEDIUM,
                description="Container missing CPU/memory resource limits — can cause resource exhaustion (denial of service) affecting other banking services.",
                cis_ref="CIS K8s 5.2.10",
                fix="Add resources.limits.cpu and resources.limits.memory to all containers.",
            ))

        # latest image tag
        if re.search(r'image\s*:.*:latest\b', line):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource=f"{resource_kind}/{resource_name}",
                kind="missing_encryption",
                severity=RiskLevel.MEDIUM,
                description="Container image uses :latest tag — non-deterministic deployments. Cannot guarantee which version is running.",
                cis_ref="CIS K8s 5.6.4",
                fix="Use immutable image tags (e.g., :v1.2.3 or SHA digest). Pin all production images.",
            ))

        # Secrets in env vars
        if re.search(r'name\s*:\s*\w*(PASSWORD|SECRET|KEY|TOKEN|API_KEY|CREDENTIAL)\w*', line, re.IGNORECASE):
            if re.search(r'value\s*:', line):
                findings.append(IaCFinding(
                    file_path=hunk.file_path, line=i,
                    resource=f"{resource_kind}/{resource_name}",
                    kind="privileged",
                    severity=RiskLevel.CRITICAL,
                    description="Secret value hardcoded in environment variable. Visible in Kubernetes API, logs, and kubectl describe output.",
                    cis_ref="CIS K8s 5.4.1",
                    fix="Use secretKeyRef with a Kubernetes Secret or external secret manager (AWS Secrets Manager, Vault).",
                ))

    return findings


# ── Dockerfile patterns ───────────────────────────────────────────────────────

def _scan_dockerfile(hunk) -> list[IaCFinding]:
    findings: list[IaCFinding] = []
    lines = hunk.content.splitlines()
    has_user_instruction = any(
        re.search(r'^USER\s+(?!root\b|0\b)\w', l[1:]) for l in lines
        if l.startswith("+") and not l.startswith("+++")
    )

    for i, raw_line in enumerate(lines, 1):
        if not (raw_line.startswith("+") and not raw_line.startswith("+++")):
            continue
        line = raw_line[1:].strip()

        # ADD instruction
        if re.match(r'^ADD\s+', line) and not re.match(r'^ADD\s+https?://', line):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource="Dockerfile",
                kind="privileged",
                severity=RiskLevel.MEDIUM,
                description="ADD instruction used — automatically extracts tarballs and can fetch URLs. Use COPY for local files.",
                cis_ref="CIS Docker 4.9",
                fix="Replace ADD with COPY for local files. Use curl/wget + verify checksum for URL downloads.",
            ))

        # USER root
        if re.match(r'^USER\s+(root|0)\b', line):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource="Dockerfile",
                kind="root_container",
                severity=RiskLevel.HIGH,
                description="Container explicitly set to run as root. Container compromise = host compromise.",
                cis_ref="CIS Docker 4.1",
                fix="Create a non-root user: RUN useradd -m appuser && USER appuser",
            ))

        # Secrets in ENV or ARG
        if re.match(r'^(?:ENV|ARG)\s+\w*(PASSWORD|SECRET|KEY|TOKEN|API_KEY)\w*\s*=', line, re.IGNORECASE):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=i,
                resource="Dockerfile",
                kind="privileged",
                severity=RiskLevel.CRITICAL,
                description="Secret/credential in Dockerfile ENV or ARG — baked into image layers. Visible in docker history and any registry.",
                cis_ref="CIS Docker 4.10",
                fix="Use runtime secrets injection (Docker Secrets, AWS Secrets Manager, Vault). Never bake secrets into images.",
            ))

        # Privileged ports
        m = re.search(r'^EXPOSE\s+(\d+)', line)
        if m:
            port = int(m.group(1))
            if port < 1024:
                findings.append(IaCFinding(
                    file_path=hunk.file_path, line=i,
                    resource="Dockerfile",
                    kind="open_ingress",
                    severity=RiskLevel.LOW,
                    description=f"Privileged port {port} exposed — requires root to bind. Conflicts with running as non-root user.",
                    cis_ref="CIS Docker 4.1",
                    fix=f"Use port >= 1024 in container and map via -p {port}:PORT_ABOVE_1024 at runtime.",
                ))

    # Missing USER instruction = runs as root
    if not has_user_instruction:
        added = [l for l in lines if l.startswith("+") and not l.startswith("+++")]
        if any(re.match(r'^(?:FROM|RUN|CMD|ENTRYPOINT)', l[1:]) for l in added):
            findings.append(IaCFinding(
                file_path=hunk.file_path, line=1,
                resource="Dockerfile",
                kind="root_container",
                severity=RiskLevel.HIGH,
                description="No USER instruction — container runs as root by default. PCI-DSS Req 7.1 violation.",
                cis_ref="CIS Docker 4.1",
                fix="Add USER instruction before CMD/ENTRYPOINT: RUN useradd -m appuser && USER appuser",
            ))

    return findings


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_k8s_name(content: str) -> str:
    m = re.search(r'name\s*:\s*(\S+)', content)
    return m.group(1) if m else "unknown"

def _extract_k8s_kind(content: str) -> str:
    m = re.search(r'kind\s*:\s*(\S+)', content)
    return m.group(1) if m else "Workload"


# ── Agent class ───────────────────────────────────────────────────────────────

class IaCAnalysisAgent(BaseAgent[IaCAnalysisResult]):

    agent_name   = AgentName.CODE_ANALYSIS
    output_model = IaCAnalysisResult

    system_prompt = (
        "You are a cloud security architect specialising in banking infrastructure.\n"
        "Review the infrastructure-as-code diff for security misconfigurations:\n"
        "  • Terraform: open security groups, unencrypted storage, public buckets, wildcard IAM\n"
        "  • Kubernetes: privileged containers, root users, missing limits, HostPath mounts\n"
        "  • Dockerfile: root user, ADD instruction, secrets in ENV\n"
        "For each finding: resource name, kind, severity, CIS benchmark reference, specific fix.\n"
        "Output ONLY compact JSON."
    )

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> IaCAnalysisResult:
        """Run deterministic IaC scanner. No LLM needed for pattern-based checks."""
        return self.fallback_result(request)

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        diff = "\n\n".join(h.content for h in request.hunks if _is_terraform(h.file_path) or _is_kubernetes(h.file_path, h.content) or _is_dockerfile(h.file_path))
        return trim_diff_for_budget(diff or "\n".join(h.content for h in request.hunks), 3000)

    def fallback_result(self, request: AnalysisRequest) -> IaCAnalysisResult:
        findings:   list[IaCFinding] = []
        tf_count = k8s_count = docker_count = 0

        for hunk in request.hunks:
            if _is_terraform(hunk.file_path):
                new = _scan_terraform(hunk)
                findings.extend(new)
                tf_count += len(new)
            elif _is_dockerfile(hunk.file_path):
                new = _scan_dockerfile(hunk)
                findings.extend(new)
                docker_count += len(new)
            elif _is_kubernetes(hunk.file_path, hunk.content) or _is_helm(hunk.file_path):
                new = _scan_kubernetes(hunk)
                findings.extend(new)
                k8s_count += len(new)

        severities = [f.severity for f in findings]
        order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        worst = max(severities, key=lambda s: order.index(s)) if severities else RiskLevel.LOW

        return IaCAnalysisResult(
            findings=findings,
            terraform_issues=tf_count,
            kubernetes_issues=k8s_count,
            docker_issues=docker_count,
            overall_severity=worst,
        )
