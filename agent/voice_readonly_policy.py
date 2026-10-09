"""Runtime policy for the security-restricted ``voice-readonly`` profile.

The effective profile is identified from Hermes' context-local/profile home,
never from prompts or model-controlled text.  Keep this module dependency-light
so file and Kanban handlers can enforce it independently of schema filtering.
"""

from __future__ import annotations

import os
from pathlib import Path


_PROFILE_NAME = "voice-readonly"
_SECRET_DIRS = {
    ".ssh", ".aws", ".gnupg", ".gpg", ".kube", ".docker",
    "keychains", "secrets", "credentials", "private",
}
_SECRET_BASENAMES = {
    ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    "auth.json", "credentials", "credentials.json", "credentials.yaml",
    "credentials.yml", "secrets.json", "secrets.yaml", "secrets.yml",
    "id_rsa", "id_ed25519", "known_hosts", "authorized_keys",
    "voice_bridge_key", "service-account.json", "service_account.json",
}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx"}
_SECRET_NAME_MARKERS = ("credential", "secret", "private_key", "private-key", "api_key", "apikey")
_CONFIG_SUFFIXES = {".yaml", ".yml", ".json", ".toml", ".ini", ".conf", ".config"}


def effective_profile_name() -> str | None:
    """Return the profile encoded by the effective Hermes home, if any."""
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home().expanduser().resolve(strict=False)
    except Exception:
        return None
    if home.parent.name == "profiles":
        return home.name
    return "default"


def is_voice_readonly_profile() -> bool:
    return effective_profile_name() == _PROFILE_NAME


def _resolved(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def read_path_denial(path: str | os.PathLike[str], *, search: bool = False) -> str | None:
    """Return a denial reason for a secret-bearing path, otherwise ``None``.

    Existing symlinks are dereferenced by ``resolve`` before checks.  Search is
    stricter than a direct read: broad roots that can recursively include the
    user's credential stores are rejected.
    """
    if not is_voice_readonly_profile():
        return None
    try:
        resolved = _resolved(path)
    except (OSError, RuntimeError, ValueError):
        return "voice-readonly: path cannot be resolved safely"

    lowered_parts = {part.lower() for part in resolved.parts}
    name = resolved.name.lower()
    suffix = resolved.suffix.lower()
    if lowered_parts & _SECRET_DIRS:
        return "voice-readonly: access to credential/key stores is denied"
    if name in _SECRET_BASENAMES or name.startswith(".env."):
        return "voice-readonly: access to secret-bearing files is denied"
    if suffix in _SECRET_SUFFIXES or any(marker in name for marker in _SECRET_NAME_MARKERS):
        return "voice-readonly: access to credential/key files is denied"

    # Hermes/Codex/provider config commonly embeds tokens.  Deny config-shaped
    # files under private agent/provider homes while leaving normal project
    # config files readable.
    if suffix in _CONFIG_SUFFIXES and lowered_parts & {
        ".hermes", ".codex", ".claude", ".config", "profiles"
    }:
        return "voice-readonly: access to credential-bearing configuration is denied"

    if search:
        home = Path.home().resolve(strict=False)
        try:
            hermes_root = home / ".hermes"
            broad = resolved in {Path("/"), home, hermes_root.resolve(strict=False)}
        except OSError:
            broad = resolved in {Path("/"), home}
        if broad:
            return "voice-readonly: broad search roots that include secret stores are denied"
    return None
