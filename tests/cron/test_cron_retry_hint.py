"""4handy local patch: cron runs retry a transient error before declaring failure.

Cron runs gave up after one transient error (a 90 s query timeout at call 22 of 150, a Gmail 403
``rateLimitExceeded``) and marked the whole run [CRON_FAILURE]. The hint now asks for up to 2 more tries
of the same call on a transient error, and never for auth/permission/validation errors or refusals.
"""

from cron.scheduler_prompt import _CRON_HINT


def test_retry_clause_lists_transient_errors():
    for phrase in ("RETRY:", "transient error", "a timeout", "429 or 5xx", "rateLimitExceeded",
                   "connection reset", "retry the same call, up to 2 more times",
                   "before treating it as a failure"):
        assert phrase in _CRON_HINT, phrase


def test_retry_clause_excludes_non_transient_errors():
    for phrase in ("Never retry an authentication or permission error", "401",
                   "a 403 that is not a rate limit", "a validation error", "read-only shared skill",
                   "AGENT_TOKEN_DENIED"):
        assert phrase in _CRON_HINT, phrase


def test_retry_comes_before_failure_and_other_clauses_stay():
    assert _CRON_HINT.index("RETRY:") < _CRON_HINT.index("FAILURE:")
    assert _CRON_HINT.index("SILENT:") < _CRON_HINT.index("RETRY:")
    for clause in ("DELIVERY:", "SILENT:", "FAILURE:", "[CRON_FAILURE]", "RECURSION:"):
        assert clause in _CRON_HINT
    assert _CRON_HINT.endswith("]\n\n")
