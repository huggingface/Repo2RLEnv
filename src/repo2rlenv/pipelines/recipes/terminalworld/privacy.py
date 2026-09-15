"""Retained TerminalWorld transcript-screening patterns; see provenance.md."""

from __future__ import annotations

import re

_RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+\.[A-Za-z]{2,}\b")

_RE_IPV4 = re.compile(
    r"\b(?!127\.)(?!10\.)(?!172\.(?:1[6-9]|2[0-9]|3[01])\.)(?!192\.168\.)"
    r"(?:\d{1,3}\.){3}\d{1,3}\b"
)

_RE_PHONE = re.compile(r"(?<!\d)\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{1,4}[\s\-.]?\d{1,9}(?!\d)")

_RE_AWS_KEY_ID = re.compile(r"\b(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b")

_RE_AWS_SECRET = re.compile(
    r"(?i)(?:aws.?secret.?access.?key|AWS_SECRET_ACCESS_KEY)\s*[=:]\s*[A-Za-z0-9/+]{40}\b"
)

_RE_AWS_SESSION = re.compile(
    r"(?i)(?:aws.?session.?token|AWS_SESSION_TOKEN)\s*[=:]\s*[A-Za-z0-9/+=]{100,}"
)

_RE_PRIVATE_KEY = re.compile(r"-----BEGIN\s+(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")

_RE_GITHUB_TOKEN = re.compile(r"\b(?:ghp|ghs|ghu|ghr|github_pat)_[A-Za-z0-9_]{36,}\b")

_RE_GITLAB_TOKEN = re.compile(r"\bglpat-[A-Za-z0-9\-_]{20,}\b")

_RE_HF_TOKEN = re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")

_RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")

_RE_GENERIC_TOKEN = re.compile(
    r'(?i)(?:export\s+)?(?:[A-Z_]{3,}(?:_KEY|_TOKEN|_SECRET|_PASSWORD|_PASSWD|_PWD))\s*=\s*["\']?[A-Za-z0-9/+\-_.]{20,}["\']?'
)

_RE_PASSWORD_FLAG = re.compile(
    r'(?i)(?:--password|--passwd|-p\s+|PGPASSWORD=|MYSQL_ROOT_PASSWORD=|MYSQL_PASSWORD=)\s*["\']?[^\s"\']{6,}'
)

_RE_DESTRUCTIVE_RM = re.compile(
    r"\brm\s+(?:[^\n]*\s)?-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+(?:/\s|/\*|~/|~\s|~\*|\*\s)"
    r"|\brm\s+(?:[^\n]*\s)?-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+(?:/\s|/\*|~/|~\s|~\*|\*\s)"
)

_RE_FORK_BOMB = re.compile(r":\(\)\s*\{.*?:\|:.*?\}|:\(\)\{.*?:\|:.*?\}")

_RE_DISK_WIPE = re.compile(r"\bdd\b[^\n]*\bof=/dev/(?:sd[a-z]|nvme\d|xvd[a-z]|vd[a-z]|hd[a-z])\b")

_RE_MKFS = re.compile(
    r"\bmkfs(?:\.[a-z0-9]+)?\s+(?:/dev/(?:sd[a-z]|nvme\d|xvd[a-z]|vd[a-z]|hd[a-z]))\b"
)

_RE_CURL_PIPE_SHELL = re.compile(
    r"(?:curl|wget)\s[^\n]*https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}[^\n]*\|\s*(?:bash|sh|zsh|dash|python|perl|ruby)\b"
)

_CHECKS: list[tuple[str, re.Pattern]] = [
    # PII
    ("pii_email", _RE_EMAIL),
    ("pii_public_ipv4", _RE_IPV4),
    ("pii_phone", _RE_PHONE),
    # Credentials
    ("cred_aws_key_id", _RE_AWS_KEY_ID),
    ("cred_aws_secret", _RE_AWS_SECRET),
    ("cred_aws_session", _RE_AWS_SESSION),
    ("cred_private_key", _RE_PRIVATE_KEY),
    ("cred_github_token", _RE_GITHUB_TOKEN),
    ("cred_gitlab_token", _RE_GITLAB_TOKEN),
    ("cred_hf_token", _RE_HF_TOKEN),
    ("cred_jwt", _RE_JWT),
    ("cred_generic_token", _RE_GENERIC_TOKEN),
    ("cred_password_flag", _RE_PASSWORD_FLAG),
    # Malicious/destructive
    ("malicious_rm_rf", _RE_DESTRUCTIVE_RM),
    ("malicious_fork_bomb", _RE_FORK_BOMB),
    ("malicious_disk_wipe", _RE_DISK_WIPE),
    ("malicious_mkfs", _RE_MKFS),
    ("malicious_curl_pipe", _RE_CURL_PIPE_SHELL),
]


def screen(text: str) -> list[str]:
    """Return category names only; never log matched personal or credential data."""
    return [label for label, pattern in _CHECKS if pattern.search(text)]
