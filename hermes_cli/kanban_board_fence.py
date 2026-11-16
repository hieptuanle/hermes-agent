"""Per-profile kanban board fence (4handy local patch, ``kanban.board_fence``; LOCAL_PATCHES.md).

Boards are shared files under one root, so any caller that names a board can open it. The fence
binds callers per profile:

* Rule 0: an agent with the kanban toolset and no setting is refused, unless it is the ``default``
  profile (an operator tool) or says ``"*"`` (any board, on purpose).
* Rules 1-2: a fenced agent's kanban tools default to its board and refuse any other
  (``tools/kanban_tools.py``, the ``_kanban_handler`` wrapper). Dispatched workers keep the
  dispatcher's pin and skip this check.
* Rule 3: a fenced profile hands cards (``kanban_create.assignee``,
  ``kanban_request_review.reviewer``) only to profiles fenced to the same board, and creates no
  triage cards (their assignees are picked later from every profile).
* Rule 4: a fenced profile's gateway refuses to run a card from another board
  (``gateway/session_kanban.py``).
* Rules 5-6: ``/kanban`` in chat is off on a fenced profile, and on every other profile unless its
  ``config.yaml`` sets ``kanban.chat_command: true`` (``gateway/slash_commands.py``).

Bad settings fail closed. The CLI, the dashboard and machine flows (dispatcher, notifier) are not
fenced: they are operator tools.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ANY_BOARD = "*"
NOT_ENABLED = "/kanban is not enabled on this bot."


@dataclass(frozen=True)
class Fence:
    """One profile's ``kanban.board_fence``: ``board`` when fenced, ``any`` for ``"*"``, ``error``
    when the setting cannot be used (callers fail closed); all empty when unset."""

    board: Optional[str] = None
    any: bool = False
    error: Optional[str] = None

    @property
    def fenced(self) -> bool:
        return self.board is not None

    def describe(self) -> str:
        if self.error:
            return f"unusable: {self.error}"
        if self.board:
            return f"'{self.board}'"
        return '"*"' if self.any else "unset"


UNSET = Fence()


class _Unreadable(Exception):
    """A home's ``config.yaml`` exists but cannot be read or parsed as a mapping."""


def parse_fence(value: Any) -> Fence:
    """``None``/``""`` → unset; ``"*"`` → any board; a board slug (lowercased, trimmed) → fenced;
    anything else → error."""
    if value is None:
        return UNSET
    text = value.strip() if isinstance(value, str) else None
    if text == "":
        return UNSET
    if text == ANY_BOARD:
        return Fence(any=True)
    if text is not None:
        from hermes_cli.kanban_db import _normalize_board_slug
        try:
            return Fence(board=_normalize_board_slug(text))
        except ValueError:
            pass
    return Fence(error=f"kanban.board_fence {value!r} is invalid (a board slug, or \"*\")")


def kanban_section(config: Any) -> dict:
    section = config.get("kanban") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def fence_from_config(config: Any) -> Fence:
    return parse_fence(kanban_section(config).get("board_fence"))


def stricter(first: Fence, second: Fence) -> Fence:
    """Two readings of one profile's setting (a session's frozen config and the live file): an
    error wins, then a board, then ``"*"``; two different boards are an error."""
    for fence in (first, second):
        if fence.error:
            return fence
    boards = {fence.board for fence in (first, second) if fence.board}
    if len(boards) > 1:
        return Fence(error="kanban.board_fence differs between this session's config "
                           f"({first.board!r}) and config.yaml ({second.board!r})")
    if boards:
        return Fence(board=boards.pop())
    return Fence(any=True) if first.any or second.any else UNSET


def _read_home_config(home: Path) -> dict:
    """``<home>/config.yaml`` as written, read from the file itself: never a managed worker's frozen
    snapshot, which describes only the calling profile. A missing file is ``{}``."""
    from hermes_cli.config_backend import read_config_doc_readonly
    path = Path(home) / "config.yaml"
    try:
        data = read_config_doc_readonly(path)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise _Unreadable(f"{path} cannot be read ({type(exc).__name__})") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _Unreadable(f"{path} is not a mapping")
    return data


def _mentions_fence(home: Path) -> bool:
    try:
        return "board_fence" in (Path(home) / "config.yaml").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False
    except OSError:
        return True  # cannot tell: assume it may


def home_fence(home: Path, *, strict: bool) -> Fence:
    """The fence a home's ``config.yaml`` sets. An unreadable file is an error when *strict*;
    otherwise only when its text mentions ``board_fence`` (so a broken file on a bot that was
    never fenced keeps today's behaviour)."""
    try:
        return fence_from_config(_read_home_config(home))
    except _Unreadable as exc:
        if strict or _mentions_fence(home):
            return Fence(error=str(exc))
        return UNSET


def profile_home(name: str) -> Optional[Path]:
    """The home of an installed profile, else ``None``."""
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists
    try:
        canon = normalize_profile_name(name)
        return get_profile_dir(canon) if profile_exists(canon) else None
    except ValueError:
        return None


def profile_fence(name: str) -> Fence:
    """Another profile's fence, read from its own file (rule 3). Unknown or unreadable → error."""
    home = profile_home(name)
    if home is None:
        return Fence(error=f"profile {name!r} is not installed")
    return home_fence(home, strict=True)


def caller_fence() -> Fence:
    """The fence of the profile this call runs for: its loaded config (a managed worker's frozen
    session config) and its live ``config.yaml``, the stricter of the two. A fence set after a
    session opened still binds it; one removed later does not loosen an open session."""
    from hermes_cli.config import load_config_readonly
    from hermes_constants import get_hermes_home
    try:
        loaded = fence_from_config(load_config_readonly())
        live = home_fence(get_hermes_home(), strict=False)
    except Exception as exc:  # fail closed
        logger.warning("kanban board fence: config could not be read", exc_info=True)
        return Fence(error=f"the profile's config could not be read ({type(exc).__name__})")
    return stricter(loaded, live)


def caller_is_default_profile() -> bool:
    """True for the root (``default``) home: an operator tool, exempt from rule 0."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() == "default"
    except Exception:
        return False


def card_refusal(home: Path, board: str, profile: str) -> Optional[tuple[str, str]]:
    """Rule 4: ``(reason, detail)`` when the profile at *home* must not run a card from *board*."""
    fence = home_fence(home, strict=False)
    if fence.error:
        return "board_fence_invalid", f"profile '{profile}': {fence.error}"
    if fence.fenced and board != fence.board:
        return "board_fenced", f"card on board '{board}'; profile '{profile}' is fenced to '{fence.board}'"
    return None


def chat_command_refusal(home: Optional[Path]) -> Optional[str]:
    """Rules 5-6: ``None`` when ``/kanban`` may run for the bot owned by *home*, else the reply.
    No owner, an unreadable file or a bad fence → refused."""
    if home is None:
        return NOT_ENABLED
    try:
        config = _read_home_config(home)
    except _Unreadable:
        return NOT_ENABLED
    fence = fence_from_config(config)
    if fence.fenced:
        return f"/kanban is off on this bot: it works only on board '{fence.board}'. Ask the bot instead."
    if fence.error or kanban_section(config).get("chat_command") is not True:
        return NOT_ENABLED
    return None
