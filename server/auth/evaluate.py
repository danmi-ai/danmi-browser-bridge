"""Evaluate command authorization.

Server-side gate for the high-risk ``evaluate`` command. Per-user opt-in
plus optional per-domain allowlist.
"""

from __future__ import annotations

from urllib.parse import urlparse

from server.storage.database import Database


class EvaluateNotAllowed(Exception):
    """Raised when the user is not authorized to run ``evaluate`` on a URL."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _domain_match(host: str, pattern: str) -> bool:
    host = (host or "").lower().strip()
    pattern = pattern.lower().strip()
    if not pattern:
        return False
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return host == suffix or host.endswith("." + suffix)
    return host == pattern


def _domain_in_allowlist(host: str, allowlist_csv: str) -> bool:
    if not allowlist_csv:
        return False
    parts = [p.strip() for p in allowlist_csv.split(",") if p.strip()]
    return any(_domain_match(host, p) for p in parts)


async def assert_evaluate_allowed(
    db: Database, user_id: str, target_url: str | None
) -> None:
    """Raise EvaluateNotAllowed if user can't run evaluate against ``target_url``.

    Caller must already have validated the user_token; this only checks the
    policy bits.
    """
    row = await db.fetchone(
        "SELECT evaluate_enabled, evaluate_domains FROM users WHERE id = ?",
        (user_id,),
    )
    if row is None:
        raise EvaluateNotAllowed("user_not_found")
    if not row["evaluate_enabled"]:
        raise EvaluateNotAllowed("evaluate_disabled_for_user")

    allowlist = row["evaluate_domains"] or ""
    if not allowlist:
        raise EvaluateNotAllowed("no_domains_allowlisted")
    if allowlist.strip() == "*":
        return  # superuser

    if not target_url:
        # We need a URL to match against the allowlist.
        raise EvaluateNotAllowed("target_url_unknown_for_allowlist_check")

    # Parse once up front so scheme checks happen before host check —
    # otherwise about:blank (Playwright internal new-page state) hits the
    # `not host: invalid_target_url` branch first and blocks every internal
    # navigation when the allowlist is non-* (was breaking auto-attach).
    try:
        parsed = urlparse(target_url)
    except Exception:
        raise EvaluateNotAllowed("invalid_target_url") from None
    scheme = (parsed.scheme or "").lower()

    # Playwright-internal schemes: about:blank / about:srcdoc / about:newtab
    # — not real navigations, just placeholder states. Always allow so
    # auto-attach + new_page() continue to work under domain restrictions.
    if scheme == "about":
        return

    # Forbidden schemes: real attempts to load privileged / dangerous content.
    if scheme in ("javascript", "data", "chrome", "chrome-extension", "file",
                  "edge", "view-source"):
        raise EvaluateNotAllowed(f"scheme_not_allowed:{scheme}")

    host = parsed.hostname or ""
    if not host:
        raise EvaluateNotAllowed("invalid_target_url")

    if not _domain_in_allowlist(host, allowlist):
        raise EvaluateNotAllowed(f"domain_not_in_allowlist:{host}")
