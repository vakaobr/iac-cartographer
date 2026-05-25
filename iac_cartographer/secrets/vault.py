"""HashiCorp Vault SecretsProvider — KV v2 reads via the REST API.

Auth: token-based, via the `VAULT_TOKEN` env var (the canonical Vault
auth pattern — works for static tokens, AppRole-derived tokens, k8s
auth tokens, AWS auth tokens, etc., whoever is responsible for
producing the token writes it to that env var before iac-cartographer
runs).

Paths: KV v2 reads at `{addr}/v1/{mount}/data/{logical_name}` and the
useful payload is at `.data.data`. We strip any leading
`iac-cartographer/` prefix from logical names before joining with
`mount + path_prefix` so operators can organise Vault paths as they
like without leaking that convention into iac-cartographer:

  * `mount = "secret"`, `path_prefix = "iac-cartographer/"` →
    `secret/data/iac-cartographer/confluence`.
  * `mount = "kv"`, `path_prefix = ""` →
    `kv/data/confluence` (operator strips the prefix at the Vault layer).

Parameters: same scheme as secrets, but the response is expected to
have a single `"value"` field whose contents are returned as a plain
string. That convention matches Vault's `kv put secret/foo value=bar`
ergonomics for single-value entries.

No new dependency — uses httpx (already pulled in for the discovery
HTTP clients)."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from iac_cartographer.constants import CartographerError
from iac_cartographer.secrets.base import SecretsProvider

logger = logging.getLogger("iac_cartographer.secrets.vault")


class VaultSecretsError(CartographerError):
    """Raised on Vault-side failures: missing token, missing path, 5xx."""


class VaultSecretsProvider(SecretsProvider):
    """Reads secrets + parameters from HashiCorp Vault KV v2."""

    def __init__(
        self,
        addr: str,
        *,
        mount: str = "secret",
        path_prefix: str = "iac-cartographer/",
        token: str | None = None,
        namespace: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._addr = addr.rstrip("/")
        self._mount = mount.strip("/")
        # Normalise prefix so it never starts with `/` and always ends with
        # `/` (unless empty) — saves the per-call join from doing it.
        if path_prefix and not path_prefix.endswith("/"):
            path_prefix += "/"
        self._path_prefix = path_prefix.lstrip("/")
        self._token = token if token is not None else os.environ.get("VAULT_TOKEN")
        self._namespace = namespace
        self._timeout_s = timeout_s
        self.name = f"vault@{self._addr}"

        if not self._token:
            raise VaultSecretsError(
                "vault secrets backend: no token available (set VAULT_TOKEN env var "
                "or pass token= to VaultSecretsProvider)"
            )

    def get_secret(self, name: str) -> dict[str, Any]:
        payload = self._read(name)
        if not isinstance(payload, dict):
            raise VaultSecretsError(
                f"vault secrets backend: secret {name!r} payload is not a JSON object (got {type(payload).__name__})"
            )
        return payload

    def get_parameter(self, name: str) -> str:
        payload = self._read(name)
        if not isinstance(payload, dict) or "value" not in payload:
            raise VaultSecretsError(
                f"vault secrets backend: parameter {name!r} must be stored as "
                f"{{'value': '<string>'}} (got keys: {sorted(payload) if isinstance(payload, dict) else payload})"
            )
        value = payload["value"]
        if not isinstance(value, str):
            raise VaultSecretsError(
                f"vault secrets backend: parameter {name!r} `value` field must be a string (got {type(value).__name__})"
            )
        return value

    # ─── internals ─────────────────────────────────────────────────────

    def _read(self, name: str) -> Any:
        path = self._mangle(name)
        url = f"{self._addr}/v1/{self._mount}/data/{path}"
        headers = {"X-Vault-Token": self._token or ""}
        if self._namespace:
            headers["X-Vault-Namespace"] = self._namespace

        try:
            resp = httpx.get(url, headers=headers, timeout=self._timeout_s)
        except httpx.HTTPError as exc:
            raise VaultSecretsError(f"vault secrets backend: HTTP error reading {url}: {exc}") from exc

        if resp.status_code == 404:
            raise VaultSecretsError(
                f"vault secrets backend: path {self._mount}/data/{path} not found (maps from logical name {name!r})"
            )
        if resp.status_code == 403:
            raise VaultSecretsError(
                f"vault secrets backend: forbidden reading {self._mount}/data/{path} — check token policy"
            )
        if resp.status_code >= 400:
            raise VaultSecretsError(
                f"vault secrets backend: read {self._mount}/data/{path} failed "
                f"(status={resp.status_code}): {resp.text[:200]}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise VaultSecretsError(f"vault secrets backend: response was not JSON: {exc}") from exc

        # KV v2 response shape: {"data": {"data": <user-payload>, "metadata": {...}}}
        inner = (payload or {}).get("data") or {}
        return inner.get("data")

    def _mangle(self, name: str) -> str:
        """Translate a logical name into the Vault KV v2 path segment.

        Strips the conventional `iac-cartographer/` prefix and joins with
        the configured `path_prefix`."""
        stripped = name.lstrip("/")
        if stripped.startswith("iac-cartographer/"):
            stripped = stripped[len("iac-cartographer/") :]
        return self._path_prefix + stripped
