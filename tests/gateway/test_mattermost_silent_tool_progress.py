"""4handy: Mattermost tool-progress posts are marked silent; other platforms untouched."""
from gateway.config import Platform
from gateway.run import _tool_progress_metadata


def test_mattermost_progress_marked_silent():
    assert _tool_progress_metadata({"thread_id": "r"}, platform=Platform.MATTERMOST) == {"thread_id": "r", "silent": True}
    assert _tool_progress_metadata(None, platform="mattermost") == {"silent": True}


def test_other_platforms_untouched():
    meta = {"thread_id": "r"}
    assert _tool_progress_metadata(meta, platform=Platform.TELEGRAM) is meta
    assert _tool_progress_metadata(None, platform=Platform.SLACK) is None


def test_progress_builder_applies_the_helper():
    """Guards against a rebase silently dropping the call site (the helper alone does nothing)."""
    import inspect
    from gateway import run_turn
    assert ("_progress_metadata = _tool_progress_metadata(_progress_metadata, platform=source.platform)"
            in inspect.getsource(run_turn))
