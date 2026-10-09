"""Runtime policy for the security-restricted ``voice-readonly`` profile.

The effective profile is identified from Hermes' context-local/profile home,
never from prompts or model-controlled text.  Keep this module dependency-light
so file and Kanban handlers can enforce it independently of schema filtering.

Design notes (from independent review, 2026-10-09):

* The boundary is **fail-closed**.  If the effective profile cannot be
  determined, we assume the restricted profile rather than silently disabling
  every check (review finding M1).
* Name matching normalises *backup tails* (``auth.json.back.20260723_182759``)
  and inspects every leading segment of the file name, because the house
  convention ``cp file file.back.$(date ...)`` produced 600+ readable copies of
  secret files that a basename denylist missed entirely (review finding H1).
* ``search`` roots are validated against an **allowlist** of trees the voice
  tier legitimately needs, not against a denylist of three broad paths — the
  previous check let ``/home`` and ``/home/tomek/..`` through (finding H2).
"""

from __future__ import annotations

import os
import re
from pathlib import Path


_PROFILE_NAME = "voice-readonly"
_SECRET_DIRS = {
    ".ssh", ".aws", ".gnupg", ".gpg", ".kube", ".docker",
    "keychains", "secrets", "credentials", "private",
}
_SECRET_BASENAMES = {
    ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    ".pgpass", ".my.cnf", ".bash_history", ".zsh_history", ".python_history",
    ".claude.json", ".histfile",
    "auth.json", "credentials", "credentials.json", "credentials.yaml",
    "credentials.yml", "secrets.json", "secrets.yaml", "secrets.yml",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "known_hosts",
    "authorized_keys", "shadow", "htpasswd", ".htpasswd",
    "voice_bridge_key", "service-account.json", "service_account.json",
    "token", "token.txt", "tokens.json", "password", "password.txt",
    "passwords.txt", "passwd", ".pgpass",
}
_SECRET_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx",
    ".ovpn", ".asc", ".gpg", ".kwallet", ".crt_key",
}
_SECRET_NAME_MARKERS = (
    "credential", "secret", "private_key", "private-key", "privatekey",
    "api_key", "apikey", "api-key", "token", "passwd", "password",
    "oauth", "refresh_token", "refresh-token", "bearer", "keyring",
    "keystore", ".env",
)
_CONFIG_SUFFIXES = {".yaml", ".yml", ".json", ".toml", ".ini", ".conf", ".config", ".env"}
_PRIVATE_HOMES = {".hermes", ".codex", ".claude", ".config", "profiles", ".gemini", ".cursor"}

# Tails produced by backup/versioning conventions.  Stripped before matching so
# `auth.json.back.20260723_182759` is judged as `auth.json`.
_BACKUP_TAIL = re.compile(
    r"(?:"
    r"[._-](?:back|backup|bak|orig|origin|save|saved|old|older|copy|prev|"
    r"previous|applied|rej|tmp|temp|swp|dist|sample|example|disabled|"
    r"before|after|snapshot|snap|rollback|v\d+)"
    r"|~\d*"                            # N5: `auth.json~`, `auth.json~1`
    r"|[._-]\d{4,}"                     # timestamps / epoch-ish suffixes
    r"|[._-]\d{8}_\d{6}"
    r")+$",
    re.IGNORECASE,
)


def _name_variants(name: str) -> set[str]:
    """Return the name plus every normalised form a denylist should see.

    Covers backup tails, trailing numeric stamps and every leading dotted
    segment, e.g. ``config.yaml.back.20260716_124703`` yields ``config.yaml``
    and ``config``; ``.claude.json.backup.178...`` yields ``.claude.json``.
    """
    lowered = name.lower()
    variants = {lowered}
    # Repeatedly strip backup tails: real files carry several (`.back.<ts>`).
    current = lowered
    for _ in range(8):
        stripped = _BACKUP_TAIL.sub("", current)
        if stripped == current or not stripped:
            break
        current = stripped
        variants.add(current)
    # Every leading dotted prefix, preserving a leading dot for dotfiles.
    for candidate in list(variants):
        lead = "." if candidate.startswith(".") else ""
        parts = candidate.lstrip(".").split(".")
        for i in range(1, len(parts) + 1):
            variants.add(lead + ".".join(parts[:i]))
    return {v for v in variants if v}


def _suffix_variants(variants: set[str]) -> set[str]:
    out = set()
    for v in variants:
        suffix = Path(v).suffix.lower()
        if suffix:
            out.add(suffix)
    return out


def effective_profile_name() -> str | None:
    """Return the profile encoded by the effective Hermes home.

    Returns ``None`` only when the profile genuinely cannot be determined;
    callers must treat ``None`` as *restricted*, never as *unrestricted*.
    """
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home().expanduser().resolve(strict=False)
    except Exception:
        return None
    if home.parent.name == "profiles":
        return home.name
    return "default"


def _fallback_looks_restricted() -> bool:
    """Best-effort check used when the profile cannot be resolved normally."""
    for var in ("HERMES_PROFILE", "HERMES_HOME", "HERMES_PROFILE_HOME"):
        if _PROFILE_NAME in (os.environ.get(var) or ""):
            return True
    return False


def is_voice_readonly_profile() -> bool:
    """True for the restricted profile; **fail-closed** when unknown."""
    name = effective_profile_name()
    if name is None:
        # Boundary must not disappear because an import or resolve blew up.
        # Unknown identity is treated as restricted (review finding M1).
        return True
    if name == _PROFILE_NAME:
        return True
    return _fallback_looks_restricted()


def _resolved(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def allowed_search_roots() -> list[Path]:
    """Trees the voice tier may recursively search.

    Allowlist, not denylist: anything outside these trees is refused, so a new
    secret file in an unexpected place is safe by default.
    """
    candidates: list[Path] = []
    for var in ("HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT"):
        value = os.environ.get(var)
        if value:
            candidates.append(Path(value))
    candidates.append(Path.home() / "projects")
    candidates.append(Path.home() / "scripts")
    out = []
    for candidate in candidates:
        try:
            out.append(candidate.expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            continue
    return out


def suppress_omitted_counts() -> bool:
    """Whether result-omission counters must be hidden from the model.

    The ``_omitted`` counter in ``search_files`` is a per-query oracle on the
    content of files the profile may not read (review finding H2): a model can
    binary-search a secret by pattern and read the count.  For the restricted
    profile the response must be indistinguishable from "no matches".
    """
    return is_voice_readonly_profile()


def read_path_denial(path: str | os.PathLike[str], *, search: bool = False) -> str | None:
    """Return a denial reason for a secret-bearing path, otherwise ``None``.

    Existing symlinks are dereferenced by ``resolve`` before checks.  Search is
    stricter than a direct read: the root must sit inside an allowed tree.
    """
    if not is_voice_readonly_profile():
        return None
    try:
        resolved = _resolved(path)
    except (OSError, RuntimeError, ValueError):
        return "voice-readonly: path cannot be resolved safely"

    lowered_parts = {part.lower() for part in resolved.parts}
    variants = _name_variants(resolved.name)
    suffixes = _suffix_variants(variants) or {resolved.suffix.lower()}

    if lowered_parts & _SECRET_DIRS:
        return "voice-readonly: access to credential/key stores is denied"
    if variants & _SECRET_BASENAMES or any(v.startswith(".env") for v in variants):
        return "voice-readonly: access to secret-bearing files is denied"
    if suffixes & _SECRET_SUFFIXES or any(
        marker in v for v in variants for marker in _SECRET_NAME_MARKERS
    ):
        return "voice-readonly: access to credential/key files is denied"

    # Hermes/Codex/provider config commonly embeds tokens.  Deny config-shaped
    # files under private agent/provider homes while leaving normal project
    # config files readable.
    if (suffixes & _CONFIG_SUFFIXES) and (lowered_parts & _PRIVATE_HOMES):
        return "voice-readonly: access to credential-bearing configuration is denied"

    if search:
        roots = allowed_search_roots()
        inside = any(resolved == root or root in resolved.parents for root in roots)
        if not inside:
            return (
                "voice-readonly: search roots are limited to the task workspace "
                "and project trees"
            )
    return None
