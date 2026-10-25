"""Redacted exception metadata safe to retain in task and experiment records."""

from __future__ import annotations

import re

_SAFE_ACP_DIAGNOSTICS = {
    "connection_refused",
    "connection_reset",
    "dns_failure",
    "timeout",
    "authentication_rejected",
    "permission_rejected",
    "budget_rejected",
    "rate_limited",
    "provider_unavailable",
}


def safe_exception_type_chain(error: BaseException, *, limit: int = 4) -> str:
    """Return exception types and bounded protocol metadata, never exception text."""
    names: list[str] = []
    current: BaseException | None = error
    while current is not None and len(names) < limit:
        name = type(current).__name__[:80]
        code = getattr(current, "code", None)
        if name == "RequestError" and isinstance(code, int) and not isinstance(code, bool):
            metadata = [f"code={code}"]
            data = getattr(current, "data", None)
            if isinstance(data, dict):
                for key in ("type", "name", "error_code"):
                    value = data.get(key)
                    if isinstance(value, str) and re.fullmatch(
                        r"[A-Za-z][A-Za-z0-9_.-]{0,79}", value
                    ):
                        metadata.append(f"{key}={value}")
                for key in ("status", "status_code"):
                    value = data.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                        metadata.append(f"{key}={value}")
            name = f"{name}[{','.join(metadata)}]"
        diagnostics = getattr(current, "harnessa_diagnostics", None)
        if isinstance(diagnostics, list):
            safe = sorted(
                item
                for item in diagnostics
                if isinstance(item, str) and item in _SAFE_ACP_DIAGNOSTICS
            )
            if safe:
                name = f"{name}[stderr={','.join(safe)}]"
        names.append(name)
        current = current.__cause__ or current.__context__
    return "->".join(names)
