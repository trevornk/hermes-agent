"""Turn-result exit policy shared by CLI views and authority-owned workers.

Worker identity is supplied by the caller, not inferred from a daemon's environment.
"""

# Provider walls do not spend a Kanban task's retry budget.
TRANSIENT_PROVIDER_REASONS = frozenset({
    "rate_limit", "upstream_rate_limit", "billing", "overloaded", "server_error", "timeout",
})
# These failures need an operator change; the dispatcher parks the task immediately.
TERMINAL_PROVIDER_REASONS = frozenset({
    "auth", "auth_permanent", "model_not_found", "ssl_cert_verification", "upstream_blocked",
})


def turn_exit_code(
    result, *, kanban_worker: bool, credentials_rate_limited: bool = False,
    credentials_terminal: bool = False, owner_unavailable: bool = False,
    transient_reasons=TRANSIENT_PROVIDER_REASONS, terminal_reasons=TERMINAL_PROVIDER_REASONS,
) -> int:
    """Preserve completion, interruption and retryable/terminal provider outcomes.

    ``owner_unavailable``: the worker's owner link failed (no receipt, no owner to dial) and the
    turn did not complete because of it — the card's infrastructure outcome, never a provider or
    credential verdict. A completed turn is still 0; a provider-terminal result stays terminal."""
    if not isinstance(result, dict):
        if credentials_rate_limited and kanban_worker:
            from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE
            return KANBAN_RATE_LIMIT_EXIT_CODE
        if credentials_terminal and kanban_worker:
            from hermes_cli.kanban_db import KANBAN_TERMINAL_PROVIDER_EXIT_CODE
            return KANBAN_TERMINAL_PROVIDER_EXIT_CODE
        if owner_unavailable and kanban_worker:
            from hermes_cli.kanban_owner_recovery import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
            return KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
        return 1
    if result.get("interrupted"):
        if owner_unavailable and kanban_worker:
            from hermes_cli.kanban_owner_recovery import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
            return KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
        return 130
    if not (result.get("failed") or result.get("partial") or result.get("completed") is False):
        return 0
    if kanban_worker:
        reason = result.get("failure_reason")
        if reason in terminal_reasons:
            from hermes_cli.kanban_db import KANBAN_TERMINAL_PROVIDER_EXIT_CODE
            return KANBAN_TERMINAL_PROVIDER_EXIT_CODE
        if owner_unavailable:
            from hermes_cli.kanban_owner_recovery import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
            return KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
        if reason in transient_reasons:
            from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE
            return KANBAN_RATE_LIMIT_EXIT_CODE
    return 1


def owner_transport_failed(agent=None, exc=None) -> bool:
    """True when the worker's owner link, not its work, ended the attempt.

    Evidence is typed, never a log tail: the agent's recorded persistence cause for this turn
    (``_last_persistence_error_cause`` / ``_lease_refresh_failure_cause``, both classified by
    ``classify_persistence_error``) or an ``owner_transport`` error in ``exc``'s cause chain."""
    from hermes_state_errors import classify_persistence_error
    for attr in ("_last_persistence_error_cause", "_lease_refresh_failure_cause"):
        if agent is not None and getattr(agent, attr, None) == "owner_transport":
            return True
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if classify_persistence_error(exc) == "owner_transport":
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def credential_failure_flags(exc) -> dict:
    """``turn_exit_code`` credential kwargs for an exception raised before any turn result exists.

    Same predicates as the one-shot CLI's ``_ensure_runtime_credentials``: a quota/rate-limit
    ``AuthError`` is transient, only an explicit re-authentication requirement is terminal. The
    cause chain is walked because agent construction wraps the resolver's error."""
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, AuthError):
            return {"credentials_rate_limited": is_rate_limited_auth_error(exc),
                    "credentials_terminal": bool(exc.relogin_required)}
        exc = exc.__cause__ or exc.__context__
    return {}
