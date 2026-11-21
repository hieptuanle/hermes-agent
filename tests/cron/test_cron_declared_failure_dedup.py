"""4handy local patch: agent-declared failures dedup on job + failing step, not on the wording.

huongtabot ``12b06a99607d`` (*/5) stayed late and unfinished on 2026-11-21: every run declared
``[CRON_FAILURE]`` with a freshly worded explanation, the incident ledger signed the text, so each run
minted a new incident and the user got a notice every 5 minutes (7 between 08:32 and 09:01). Declared
failures now share one incident per job + failing step (``_classify_failure_type`` of the explanation):
one notice, repeats withheld for ``cron.failure_repeat_alert_hours``, and the next successful run
resolves it so the next streak alerts again. Runtime errors keep the text signature.
"""

import pytest

import cron.scheduler as sched
from cron import incidents

JOB = {"id": "12b06a99607d", "name": "CK deadline", "deliver": "mattermost"}
LATE = [
    "Báo cáo ngày 20/11 chưa hoàn tất; đã trễ hạn 08:00. 0/57 đơn đã đối chiếu.",
    "Báo cáo CK ngày 20/11 chưa hoàn tất; hạn 08:00 đã lỡ. Đã xác minh 58/58 cặp nguồn.",
    "Chưa hoàn tất báo cáo ngày 20/11; checkpoint ghi 0/57 đơn đã adjudicate.",
    "Mới đối chiếu xong 1/57 đơn đủ điều kiện; còn 56 đơn chưa kiểm tra xong.",
]
AUTH = "Google OAuth trả 401 unauthorized (invalid_grant); cần đăng nhập lại."


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    (tmp_path / "config.yaml").write_text(
        "cron:\n  declared_failure_delivery: verbatim\n", encoding="utf-8")
    return tmp_path


def _declared(text, job=JOB):
    """One run that ended ``[CRON_FAILURE]`` + *text*: what reaches the user, booked like the real
    delivery path (a delivered ping marks the incident alerted)."""
    success, error, declared = sched._apply_agent_failure_marker(
        dict(job), True, None, f"[CRON_FAILURE]\n{text}")
    assert (success, declared) == (False, True)
    content, _blocked, _silent, _acked, inc_id = sched._compose_run_delivery(
        dict(job), success=success, error=error, final_response=f"[CRON_FAILURE]\n{text}",
        output_file=None, agent_declared=declared)
    if content.strip():
        sched._mark_incident_alerted(inc_id)
    return content, inc_id


def _runtime(error, job=JOB):
    content, _b, _s, _a, inc_id = sched._compose_run_delivery(
        dict(job), success=False, error=error, final_response="", output_file=None)
    if content.strip():
        sched._mark_incident_alerted(inc_id)
    return content, inc_id


def _ok(text="Báo cáo CK ngày 20/11: xong.", job=JOB):
    content, *_ = sched._compose_run_delivery(
        dict(job), success=True, error=None, final_response=text, output_file=None)
    return content


def test_late_run_re_declaring_every_tick_notifies_once(ledger):
    delivered = [_declared(t) for t in LATE]
    assert delivered[0][0] == LATE[0]
    assert [c for c, _ in delivered[1:]] == ["", "", ""]
    assert len({i for _, i in delivered}) == 1
    row = incidents.get_incident(delivered[0][1])
    assert row["state"] == "alerted" and row["error_sig"].startswith(incidents.DECLARED_SIG_PREFIX)
    assert row["error"] == LATE[-1]  # the ledger keeps the latest explanation


def test_recovery_delivers_the_result_and_the_next_streak_alerts_again(ledger):
    first, inc_id = _declared(LATE[0])
    assert _declared(LATE[1])[0] == ""
    assert _ok() == "Báo cáo CK ngày 20/11: xong."
    assert incidents.get_incident(inc_id)["state"] == "resolved"
    again, same_id = _declared(LATE[2])
    assert again == LATE[2] and same_id == inc_id
    assert _declared(LATE[3])[0] == ""


def test_a_different_failing_step_notifies_once_more(ledger):
    assert _declared(LATE[0])[0] == LATE[0]
    auth, auth_id = _declared(AUTH)
    assert auth == AUTH
    assert incidents.get_incident(auth_id)["failure_type"] == "auth"
    assert _declared(AUTH.replace("401", "401 lần nữa"))[0] == ""
    assert _declared(LATE[1])[0] == ""


def test_jobs_do_not_share_a_declared_incident(ledger):
    other = dict(JOB, id="12b06a000000")  # same 6-char prefix as JOB
    a, a_id = _declared(LATE[0])
    b, b_id = _declared(LATE[1], job=other)
    assert a == LATE[0] and b == LATE[1] and a_id != b_id


def test_runtime_errors_keep_the_text_signature(ledger):
    first, first_id = _runtime("Script exited 2: export failed")
    second, second_id = _runtime("Script exited 3: sheet locked")
    assert first.strip() and second.strip() and first_id != second_id
    assert not incidents.get_incident(first_id)["error_sig"].startswith(incidents.DECLARED_SIG_PREFIX)
    assert _runtime("Script exited 2: export failed")[0] == ""  # identical text: still deduped


def test_reminder_cooldown_still_applies(ledger):
    (ledger / "config.yaml").write_text(
        "cron:\n  declared_failure_delivery: verbatim\n  failure_repeat_alert_hours: 0\n",
        encoding="utf-8")
    assert [_declared(t)[0] for t in LATE[:3]] == LATE[:3]


def test_operator_ack_of_a_declared_streak_lasts_until_recovery(ledger):
    _, inc_id = _declared(LATE[0])
    incidents.ack_incident(inc_id)
    assert _declared(LATE[1])[0] == ""
    _ok()
    assert incidents.get_incident(inc_id)["state"] == "resolved"
    assert _declared(LATE[2])[0] == LATE[2]


def test_operator_ack_of_a_text_incident_still_survives_recovery(ledger):
    _, inc_id = _runtime("known flaky upstream 502")
    incidents.ack_incident(inc_id)
    _ok()
    assert incidents.get_incident(inc_id)["state"] == "closed"
    assert _runtime("known flaky upstream 502")[0] == ""


def test_declared_signature_ignores_wording_but_not_job_or_step():
    sig = incidents.declared_failure_signature
    assert sig("j1", LATE[0]) == sig("j1", LATE[1]) == sig("j1", "")
    assert sig("j1", LATE[0]) != sig("j2", LATE[0])
    assert sig("j1", LATE[0]) != sig("j1", AUTH)
    assert all(not incidents._error_signature("j1", t).startswith(incidents.DECLARED_SIG_PREFIX)
               for t in LATE + [AUTH])
