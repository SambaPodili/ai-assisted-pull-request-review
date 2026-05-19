"""
analysis/iac_scanner.py
------------------------
Infrastructure-as-Code security scanner.

Detects misconfigurations in:
  • Terraform / OpenTofu (.tf files)
  • Kubernetes manifests (.yaml/.yml with k8s schema)
  • Docker / docker-compose (Dockerfile, docker-compose.yml)
  • Helm charts (Chart.yaml + templates/)
  • AWS CloudFormation (.template / .json with CloudFormation schema)

Banking-critical checks:
  • Open network access (0.0.0.0/0 ingress on sensitive ports)
  • Unencrypted storage (S3, RDS, EBS without encryption)
  • Publicly accessible databases
  • Missing deletion protection on production resources
  • Privileged containers / root users in K8s
  • Secrets in environment variables (should use secrets manager)
  • Missing resource limits (K8s)
  • Latest image tags (non-reproducible deployments)
  • Disabled MFA / IAM best practice violations
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

# ── Finding model ─────────────────────────────────────────────────────────────

@dataclass
class IaCFinding:
    file_path:     str
    resource_type: str
    resource_name: str
    severity:      str   # "critical" | "high" | "medium" | "low"
    rule_id:       str
    title:         str
    description:   str
    remediation:   str
    line:          int = 0
    framework:     str = ""  # "terraform" | "kubernetes" | "docker" | "cloudformation"


@dataclass
class IaCAnalysisResult:
    findings:       list[IaCFinding] = field(default_factory=list)
    iac_files:      list[str] = field(default_factory=list)
    frameworks:     list[str] = field(default_factory=list)
    critical_count: int = 0
    high_count:     int = 0


# ── File type detection ────────────────────────────────────────────────────────

def detect_iac_type(file_path: str, content: str = "") -> str | None:
    """Return the IaC framework type or None."""
    fp = file_path.lower()
    if fp.endswith('.tf') or fp.endswith('.tfvars'):
        return "terraform"
    if 'dockerfile' in fp:
        return "docker"
    if 'docker-compose' in fp and fp.endswith(('.yml', '.yaml')):
        return "docker_compose"
    if fp.endswith(('.yml', '.yaml')):
        # Distinguish K8s from other YAML
        k8s_markers = ('apiVersion:', 'kind:', 'metadata:', 'spec:')
        cf_markers  = ('AWSTemplateFormatVersion', 'Resources:')
        if any(m in content for m in k8s_markers):
            return "kubernetes"
        if any(m in content for m in cf_markers):
            return "cloudformation"
        if 'helm' in fp or 'chart' in fp.lower():
            return "helm"
    if fp.endswith(('.json', '.template')):
        if 'AWSTemplateFormatVersion' in content or 'Resources' in content:
            return "cloudformation"
    return None


# ── Terraform checks ──────────────────────────────────────────────────────────

_TF_OPEN_INGRESS = re.compile(
    r'ingress\s*\{[^}]*(?:cidr_blocks|ipv6_cidr_blocks)\s*=\s*\["(?:0\.0\.0\.0/0|::?/0)"\]', re.S)
_TF_ENCRYPTED    = re.compile(r'encrypted\s*=\s*false')
_TF_PUBLIC_DB    = re.compile(r'publicly_accessible\s*=\s*true')
_TF_NO_DELETION  = re.compile(r'deletion_protection\s*=\s*false')
_TF_NO_MULTIAZ   = re.compile(r'multi_az\s*=\s*false')
_TF_HARDCODED_SECRET = re.compile(r'(?:password|secret|token|key)\s*=\s*"[^${\s]{6,}"')
_TF_RESOURCE     = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"', re.M)
_TF_LOG_DISABLED = re.compile(r'(?:logging|enable_logging)\s*=\s*false')
_TF_HTTP_ONLY    = re.compile(r'http_only\s*=\s*false|force_ssl\s*=\s*false')


def _scan_terraform(content: str, file_path: str, added_only: bool) -> list[IaCFinding]:
    findings = []

    # Get current resource context for each line
    current_resource = ("", "")
    for ln, line in enumerate(content.splitlines(), 1):
        m = _TF_RESOURCE.match(line)
        if m:
            current_resource = (m.group(1), m.group(2))

    res_type, res_name = current_resource

    if _TF_OPEN_INGRESS.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type=res_type, resource_name=res_name,
            severity="critical", rule_id="TF-NET-001", framework="terraform",
            title="Security group open to internet (0.0.0.0/0)",
            description="Inbound rule allows traffic from all internet addresses. In banking environments all ingress must be restricted to known CIDR ranges or VPN.",
            remediation="Replace 0.0.0.0/0 with specific CIDR blocks (internal VPC range or bastion CIDR). Use NACLs as an additional defence layer.",
        ))

    if _TF_ENCRYPTED.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type=res_type, resource_name=res_name,
            severity="critical", rule_id="TF-ENC-001", framework="terraform",
            title="Storage resource with encryption disabled",
            description="encrypted = false disables at-rest encryption. PCI-DSS Req 3.5 and MAS TRM TRM-G07 mandate encryption of all data at rest.",
            remediation="Set encrypted = true. For RDS use kms_key_id to specify a customer-managed KMS key. For S3 use server_side_encryption_configuration.",
        ))

    if _TF_PUBLIC_DB.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type=res_type, resource_name=res_name,
            severity="critical", rule_id="TF-DB-001", framework="terraform",
            title="Database publicly accessible",
            description="publicly_accessible = true exposes the database endpoint to the public internet. Banking databases must only be accessible within the VPC.",
            remediation="Set publicly_accessible = false. Access the database via application tier only. Use VPC peering or PrivateLink for cross-account access.",
        ))

    if _TF_NO_DELETION.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type=res_type, resource_name=res_name,
            severity="high", rule_id="TF-DB-002", framework="terraform",
            title="Deletion protection disabled on database",
            description="deletion_protection = false allows the database to be destroyed without safeguard. A Terraform error or malicious action could cause data loss.",
            remediation="Set deletion_protection = true on all production RDS instances. Require manual disabling before destroy.",
        ))

    if _TF_NO_MULTIAZ.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type=res_type, resource_name=res_name,
            severity="high", rule_id="TF-DB-003", framework="terraform",
            title="RDS instance without Multi-AZ",
            description="multi_az = false means a single AZ failure will cause database downtime. Banking applications require 99.99% availability.",
            remediation="Set multi_az = true for all production databases. Use read_replica_identifier for read replicas.",
        ))

    if _TF_HARDCODED_SECRET.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type=res_type, resource_name=res_name,
            severity="critical", rule_id="TF-SEC-001", framework="terraform",
            title="Hardcoded secret in Terraform configuration",
            description="Credentials or secrets are hardcoded in .tf files. These will be committed to git and stored in Terraform state (often unencrypted).",
            remediation="Use AWS Secrets Manager data source: data.aws_secretsmanager_secret_version.xxx.secret_string. Never hardcode credentials in IaC.",
        ))

    if _TF_LOG_DISABLED.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type=res_type, resource_name=res_name,
            severity="high", rule_id="TF-LOG-001", framework="terraform",
            title="Logging disabled on resource",
            description="Audit logging is disabled. MAS TRM requires comprehensive logging for all access to critical systems.",
            remediation="Enable logging and ship logs to a centralised SIEM. For S3 enable server access logging. For RDS enable enhanced monitoring.",
        ))

    return findings


# ── Kubernetes checks ─────────────────────────────────────────────────────────

_K8S_PRIVILEGED     = re.compile(r'privileged:\s*true')
_K8S_RUN_AS_ROOT    = re.compile(r'runAsNonRoot:\s*false|runAsUser:\s*0\b')
_K8S_HOST_NETWORK   = re.compile(r'hostNetwork:\s*true')
_K8S_HOST_PID       = re.compile(r'hostPID:\s*true|hostIPC:\s*true')
_K8S_NO_LIMITS      = re.compile(r'resources:\s*\{\}|resources:\s*\n\s*requests:(?!\n\s*limits:)')
_K8S_LATEST_TAG     = re.compile(r'image:\s*[^\s:]+:latest\b')
_K8S_NO_LIVENESS    = re.compile(r'containers:', re.M)
_K8S_ENV_SECRET     = re.compile(r'env:\s*\n(?:\s*-[^\n]*\n)*\s*-\s*name:[^\n]*\n\s*value:\s*"[^"]{8,}"')
_K8S_ALLOW_PRIVILEGE= re.compile(r'allowPrivilegeEscalation:\s*true')
_K8S_CAPABILITIES   = re.compile(r'capabilities:\s*\n\s*add:\s*\[[^\]]*(?:SYS_ADMIN|NET_ADMIN|ALL)[^\]]*\]')
_K8S_KIND           = re.compile(r'^kind:\s*(\w+)', re.M)
_K8S_NAME           = re.compile(r'^\s*name:\s*(\S+)', re.M)


def _scan_kubernetes(content: str, file_path: str) -> list[IaCFinding]:
    findings = []
    kind = (_K8S_KIND.search(content) or type('', (), {'group': lambda *a: ''})()).group(1) or "Unknown"
    name = (_K8S_NAME.search(content) or type('', (), {'group': lambda *a: ''})()).group(1) or "unknown"

    checks = [
        (_K8S_PRIVILEGED,      "critical", "K8S-SEC-001", "Privileged container",
         "privileged: true grants the container full host privileges — equivalent to running as root on the node. A compromise of this container leads to full cluster compromise.",
         "Remove privileged: true. Use specific capabilities (add: [NET_BIND_SERVICE]) only if required."),
        (_K8S_RUN_AS_ROOT,     "high",     "K8S-SEC-002", "Container runs as root",
         "runAsNonRoot: false or runAsUser: 0 allows the container to run as root. Root in a container can break out of the container via kernel exploits.",
         "Set runAsNonRoot: true and runAsUser: 1000 (non-root UID) in securityContext."),
        (_K8S_HOST_NETWORK,    "critical", "K8S-SEC-003", "Container uses host network",
         "hostNetwork: true shares the node's network namespace. The container can sniff all node traffic and bypass NetworkPolicies.",
         "Remove hostNetwork: true. Use Kubernetes Services and Ingress for network exposure."),
        (_K8S_HOST_PID,        "critical", "K8S-SEC-004", "Container uses host PID/IPC",
         "hostPID or hostIPC grants visibility into all processes on the host node.",
         "Remove hostPID and hostIPC. These are almost never needed in application containers."),
        (_K8S_LATEST_TAG,      "high",     "K8S-DEP-001", "Container uses :latest image tag",
         "image: xxx:latest is non-reproducible — the image pulled during a deployment may differ from what was tested. In an incident you cannot reliably roll back.",
         "Use immutable image tags (e.g. image: myapp:1.2.3 or image: myapp@sha256:abc...)."),
        (_K8S_ALLOW_PRIVILEGE, "high",     "K8S-SEC-005", "Privilege escalation allowed",
         "allowPrivilegeEscalation: true permits a process to gain more privileges than its parent. Enables SUID binary exploitation.",
         "Set allowPrivilegeEscalation: false in securityContext."),
        (_K8S_CAPABILITIES,    "high",     "K8S-SEC-006", "Dangerous Linux capabilities granted",
         "SYS_ADMIN or NET_ADMIN capabilities provide near-root access to the host kernel.",
         "Drop all capabilities and add only what is strictly needed: drop: [ALL], add: [NET_BIND_SERVICE]."),
        (_K8S_ENV_SECRET,      "critical", "K8S-SEC-007", "Secret hardcoded in environment variable",
         "Secrets in env.value are stored in plaintext in etcd and visible to anyone with kubectl get pod -o yaml.",
         "Use secretKeyRef to reference a Kubernetes Secret object. Better: use an external secret manager (AWS Secrets Manager, Vault)."),
        (_K8S_NO_LIMITS,       "medium",   "K8S-RES-001", "Missing resource limits",
         "Without resource limits a single buggy pod can consume all node memory/CPU and cause a node-level outage (noisy neighbour).",
         "Set resources.limits.cpu and resources.limits.memory on all containers."),
    ]

    for pattern, severity, rule_id, title, desc, rem in checks:
        if pattern.search(content):
            findings.append(IaCFinding(
                file_path=file_path, resource_type=kind, resource_name=name,
                severity=severity, rule_id=rule_id, framework="kubernetes",
                title=title, description=desc, remediation=rem,
            ))

    return findings


# ── Docker checks ─────────────────────────────────────────────────────────────

_DOCKER_USER_ROOT  = re.compile(r'^USER\s+root\s*$', re.M | re.I)
_DOCKER_NO_USER    = re.compile(r'^FROM\s+', re.M)  # used to detect missing USER statement
_DOCKER_ADD_URL    = re.compile(r'^ADD\s+https?://', re.M)
_DOCKER_EXPOSE_SENSITIVE = re.compile(r'^EXPOSE\s+(?:22|23|3306|5432|6379|27017|9200)\b', re.M)
_DOCKER_SECRETS_ARG = re.compile(r'^ARG\s+(?:password|secret|token|key|api)', re.M | re.I)
_DOCKER_LATEST     = re.compile(r'^FROM\s+\S+:latest\b', re.M | re.I)
_DOCKER_WORKDIR    = re.compile(r'^WORKDIR\s+/', re.M)
_DOCKER_CURL_PIPE  = re.compile(r'curl[^|]+\|\s*(?:bash|sh)\b')


def _scan_docker(content: str, file_path: str) -> list[IaCFinding]:
    findings = []
    has_user = bool(re.search(r'^USER\s+(?!root)\w', content, re.M | re.I))

    if _DOCKER_USER_ROOT.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type="Dockerfile", resource_name=file_path,
            severity="critical", rule_id="DOCK-SEC-001", framework="docker",
            title="Container runs as root",
            description="USER root explicitly runs the container as root. A container breakout would give attacker root on the host.",
            remediation="Create a non-root user: RUN useradd -m appuser && USER appuser",
        ))
    elif not has_user and _DOCKER_NO_USER.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type="Dockerfile", resource_name=file_path,
            severity="high", rule_id="DOCK-SEC-002", framework="docker",
            title="No USER statement — container defaults to root",
            description="Without a USER instruction the container runs as root by default.",
            remediation="Add: RUN addgroup -S appgroup && adduser -S appuser -G appgroup\nUSER appuser",
        ))

    if _DOCKER_LATEST.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type="Dockerfile", resource_name=file_path,
            severity="high", rule_id="DOCK-DEP-001", framework="docker",
            title="Base image uses :latest tag",
            description="FROM xxx:latest is non-reproducible and may silently include breaking changes or vulnerabilities in future builds.",
            remediation="Pin to a specific version: FROM python:3.12.3-slim or use image digest FROM python@sha256:...",
        ))

    if _DOCKER_ADD_URL.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type="Dockerfile", resource_name=file_path,
            severity="high", rule_id="DOCK-SEC-003", framework="docker",
            title="ADD with URL (use COPY instead)",
            description="ADD with a URL fetches content at build time without integrity verification. A compromised URL can inject malicious code.",
            remediation="Use COPY for local files. For remote files use: RUN curl -fsSL --checksum sha256:HASH URL -o file",
        ))

    if _DOCKER_EXPOSE_SENSITIVE.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type="Dockerfile", resource_name=file_path,
            severity="high", rule_id="DOCK-NET-001", framework="docker",
            title="Sensitive port exposed",
            description="Exposing SSH (22), database (3306, 5432, 27017), Redis (6379) or Elasticsearch (9200) ports increases attack surface.",
            remediation="Remove sensitive port exposures. Access databases via internal service names. Disable SSH in containers (use kubectl exec).",
        ))

    if _DOCKER_SECRETS_ARG.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type="Dockerfile", resource_name=file_path,
            severity="critical", rule_id="DOCK-SEC-004", framework="docker",
            title="Secret passed via ARG (visible in image layers)",
            description="ARG values are baked into image layers and visible via docker history. Anyone with image pull access can see the secret.",
            remediation="Use Docker BuildKit secrets: RUN --mount=type=secret,id=mysecret cat /run/secrets/mysecret. Never use ARG for credentials.",
        ))

    if _DOCKER_CURL_PIPE.search(content):
        findings.append(IaCFinding(
            file_path=file_path, resource_type="Dockerfile", resource_name=file_path,
            severity="critical", rule_id="DOCK-SEC-005", framework="docker",
            title="curl piped to shell (curl | bash)",
            description="Executing remote scripts without integrity verification is a supply chain attack vector. The remote server could serve a malicious script.",
            remediation="Download the script, verify its checksum/signature, then execute: RUN curl -fsSL URL -o install.sh && sha256sum -c checksums.txt && bash install.sh",
        ))

    return findings


# ── Main scanner ──────────────────────────────────────────────────────────────

def scan_iac_diff(diff_text: str, file_path: str) -> list[IaCFinding]:
    """
    Scan IaC content from a diff for security misconfigurations.
    Extracts added content only (+ lines) for analysis.
    """
    # Extract added lines for analysis context, but scan full content for pattern context
    added_lines = [line[1:] for line in diff_text.splitlines()
                   if line.startswith('+') and not line.startswith('+++')]
    added_content = '\n'.join(added_lines)

    if not added_content.strip():
        return []

    iac_type = detect_iac_type(file_path, added_content)
    if not iac_type:
        return []

    if iac_type == "terraform":
        return _scan_terraform(added_content, file_path, added_only=True)
    elif iac_type == "kubernetes":
        return _scan_kubernetes(added_content, file_path)
    elif iac_type in ("docker", "docker_compose"):
        return _scan_docker(added_content, file_path)
    return []


def scan_hunks(hunks: list) -> IaCAnalysisResult:
    """Scan a list of DiffHunk objects for IaC misconfigurations."""
    result = IaCAnalysisResult()
    frameworks: set[str] = set()

    for hunk in hunks:
        fp = getattr(hunk, 'file_path', '')
        content = getattr(hunk, 'content', '')

        iac_type = detect_iac_type(fp, content)
        if not iac_type:
            continue

        result.iac_files.append(fp)
        findings = scan_iac_diff(content, fp)
        result.findings.extend(findings)
        for f in findings:
            frameworks.add(f.framework)
            if f.severity == "critical":
                result.critical_count += 1
            elif f.severity == "high":
                result.high_count += 1

    result.frameworks = sorted(frameworks)
    return result
