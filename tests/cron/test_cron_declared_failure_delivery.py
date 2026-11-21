"""4handy local patch: broadened [CRON_FAILURE] hint + ``cron.declared_failure_delivery``.

Cron runs whose script failed/timed out or whose report was never sent ended ``last_status=ok`` because
the hint only told the model to use ``[CRON_FAILURE]`` for a failed delegated child. And when the marker
is used, ``notice`` mode replaces the bot's own explanation with an English admin notice (+ a "pause it"
nudge from the 3rd failure). ``verbatim`` delivers the explanation as written; bookkeeping is unchanged.
"""

import pytest

import cron.scheduler as s
from cron.scheduler_failure_copy import generic_failure_notice
from cron.scheduler_prompt import _CRON_HINT

EXPLANATION = "Không gửi được báo cáo tồn kho: script xuất dữ liệu hết thời gian chờ sau 420 giây."


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _set_mode(home, value) -> None:
    (home / "config.yaml").write_text(f"cron:\n  declared_failure_delivery: {value}\n", encoding="utf-8")


def _job(**extra) -> dict:
    return {"id": "job-cf-1", "name": "Báo cáo tồn kho", "deliver": "telegram",
            "schedule": {"kind": "cron", "expr": "0 22 * * *"}, "failure_streak": 4, **extra}


@pytest.fixture
def pipeline(monkeypatch):
    """Run ``run_one_job`` with a fixed agent answer; record delivery, marks and incidents."""
    rec = {"delivered": [], "marks": [], "incidents": []}

    def run(final, *, job=None, acked=False):
        monkeypatch.setattr(s, "run_job", lambda job, **kw: (True, "out", final, None))
        monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
        monkeypatch.setattr(
            "cron.delivery_queue.enqueue",
            lambda execution_id, job, content, **kw: rec["delivered"].append(content) or {"status": "pending"})
        monkeypatch.setattr(
            s, "mark_job_run",
            lambda jid, ok, err=None, delivery_error=None, **kw: rec["marks"].append((jid, ok, err)))

        def upsert(job, error, *, output_file=None):
            rec["incidents"].append((job["id"], error))
            return acked, "inc-1"

        monkeypatch.setattr(s, "_upsert_incident_for_failure", upsert)
        s.run_one_job(job or _job())
        return rec

    return run


class TestHint:
    def test_failure_clause_covers_own_failures(self):
        for phrase in ("[CRON_FAILURE]", "first line by itself", "script or command it depends on failed",
                       "exited non-zero", "timed out", "a required file, sheet or report was not produced",
                       "a delegated child failed", "never translated", "language this job normally uses",
                       "that is [SILENT]", "partial results the task explicitly allows",
                       "name the step or error that failed"):
            assert phrase in _CRON_HINT, phrase

    def test_failure_clause_only_counts_messages_the_task_sends_with_a_tool(self):
        # 2026-11-21 duongnhungbot ed6578c7c185: the prompt said "send" the question, the clause said
        # an unsent message is a failure, so the bot returned [CRON_FAILURE] instead of the question
        # the system would have delivered. Only tool sends the task asks for count.
        failure = _CRON_HINT[_CRON_HINT.index("FAILURE:"):_CRON_HINT.index("RECURSION:")]
        assert "message was not produced or not sent" not in failure
        assert "could not produce or deliver" not in failure
        assert "a message the task tells you to send with a tool" in failure
        assert "Your final response is not such a message: the system delivers it" in failure
        assert "never declare a failure because you cannot post or send it yourself" in failure
        assert "write it as your final response instead" in failure

    def test_hint_keeps_other_clauses(self):
        assert "DELIVERY:" in _CRON_HINT and "SILENT:" in _CRON_HINT and "RECURSION:" in _CRON_HINT
        assert "If a delegated child fails and" not in _CRON_HINT


class TestMode:
    @pytest.mark.parametrize("value, expected", [
        (None, "notice"), ("notice", "notice"), ("verbatim", "verbatim"), ("VERBATIM", "verbatim"),
        ("loud", "notice"), ("''", "notice"), ("1", "notice")])
    def test_mode_resolution(self, home, value, expected):
        if value is not None:
            _set_mode(home, value)
        assert s._declared_failure_delivery_mode() == expected


class TestVerbatim:
    def test_delivers_exactly_the_explanation_and_records_failure(self, home, pipeline):
        _set_mode(home, "verbatim")
        rec = pipeline(f"[CRON_FAILURE]\n{EXPLANATION}\n")
        assert rec["delivered"] == [EXPLANATION]
        assert rec["marks"] == [("job-cf-1", False, EXPLANATION)]
        assert rec["incidents"] == [("job-cf-1", EXPLANATION)]

    def test_no_english_wrapper_or_streak_nudge(self, home, pipeline):
        _set_mode(home, "verbatim")
        rec = pipeline(f"[CRON_FAILURE]\n{EXPLANATION}", job=_job(failure_streak=9))
        text = rec["delivered"][0]
        assert "failed" not in text and "hermes cron" not in text and "pause" not in text

    def test_multiline_explanation_kept(self, home, pipeline):
        _set_mode(home, "verbatim")
        body = "Dòng 1: script lỗi (exit 2).\n\nDòng 2: chưa gửi Zalo."
        rec = pipeline(f"[CRON_FAILURE]\n{body}")
        assert rec["delivered"] == [body]

    def test_bare_marker_falls_back_to_notice(self, home, pipeline):
        _set_mode(home, "verbatim")
        rec = pipeline("[CRON_FAILURE]")
        assert rec["marks"][0][1] is False
        assert rec["delivered"][0].startswith(generic_failure_notice(
            "Báo cáo tồn kho", "job-cf-1", s.CRON_FAILURE_DEFAULT_EVIDENCE.rstrip(".")))

    def test_acked_incident_stays_silent(self, home, pipeline):
        _set_mode(home, "verbatim")
        rec = pipeline(f"[CRON_FAILURE]\n{EXPLANATION}", acked=True)
        assert rec["delivered"] == []
        assert rec["marks"][0][1] is False

    def test_silent_unchanged(self, home, pipeline):
        _set_mode(home, "verbatim")
        rec = pipeline("[SILENT]")
        assert rec["delivered"] == [] and rec["marks"][0][1] is True and rec["incidents"] == []

    def test_marker_not_on_first_line_is_a_report(self, home, pipeline):
        _set_mode(home, "verbatim")
        report = f"Báo cáo hôm nay.\n[CRON_FAILURE]\n{EXPLANATION}"
        rec = pipeline(report)
        assert rec["marks"][0][1] is True and rec["incidents"] == []
        assert rec["delivered"] == [report]

    def test_compose_used_by_receipt_recovery_honours_mode(self, home):
        """``cron/scheduler_authority.py`` books recovered runs through the same composer."""
        _set_mode(home, "verbatim")
        job = _job()
        success, error, declared = s._apply_agent_failure_marker(
            job, True, None, f"[CRON_FAILURE]\n{EXPLANATION}")
        assert (success, declared) == (False, True)
        content, *_ = s._compose_run_delivery(
            job, success=success, error=error, final_response=f"[CRON_FAILURE]\n{EXPLANATION}",
            output_file=None, agent_declared=declared)
        assert content == EXPLANATION


class TestNoticeUnchanged:
    @pytest.mark.parametrize("value", [None, "notice", "bogus"])
    def test_notice_wrapper_and_nudge(self, home, pipeline, value):
        if value is not None:
            _set_mode(home, value)
        rec = pipeline(f"[CRON_FAILURE]\n{EXPLANATION}")
        expected = generic_failure_notice("Báo cáo tồn kho", "job-cf-1", EXPLANATION.rstrip("."))
        assert rec["delivered"][0].startswith(expected)
        assert rec["delivered"][0] != expected  # streak 4 + this run >= 3: the nudge follows
        assert rec["marks"] == [("job-cf-1", False, EXPLANATION)]

    def test_runtime_failure_not_affected_by_verbatim(self, home, monkeypatch, pipeline):
        """Only agent-declared failures change; a runtime error still gets the summarizer copy."""
        _set_mode(home, "verbatim")
        delivered = []
        monkeypatch.setattr(s, "run_job", lambda job, **kw: (False, "out", "", "Script exited with code 2"))
        monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
        monkeypatch.setattr("cron.delivery_queue.enqueue",
                            lambda execution_id, job, content, **kw: delivered.append(content) or {})
        monkeypatch.setattr(s, "mark_job_run", lambda *a, **kw: None)
        monkeypatch.setattr(s, "_upsert_incident_for_failure", lambda job, error, **kw: (False, None))
        s.run_one_job(_job())
        assert delivered and delivered[0] != "Script exited with code 2"
