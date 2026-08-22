"""Tests for the declared-artifact gate on kanban completion.

A worker that runs out of iteration budget can still mark its card done,
and a review card that produced no report then looks indistinguishable
from one that produced three. This gate closes that: when a card names
the files it must leave behind, completion checks they are on disk.

Two layers:

1. Path extraction from the card body -- which shapes count as a
   declared artifact and which are prose that merely mentions a file.
2. The gate itself -- missing files are reported, present files pass,
   and cards that declare nothing are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Path extraction
# ---------------------------------------------------------------------------


def test_extracts_paths_from_artifacts_section():
    """A section naming output files yields those paths."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    body = """
## Review scope
Branch feat/x, commits HEAD~2..HEAD

## Deliverables
- docs/reviews/round1.md
- docs/reviews/round2.md
"""
    assert _declared_artifact_paths(body) == [
        "docs/reviews/round1.md",
        "docs/reviews/round2.md",
    ]


def test_ignores_prose_mentioning_files_outside_a_deliverables_section():
    """A path in the background prose is not a deliverable."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    body = """
## Background
The link failure comes from tools/image-host.c referencing image-sig.c.
Read configs/imx8qxp_defconfig for the current settings.
"""
    assert _declared_artifact_paths(body) == []


def test_recognises_the_common_heading_spellings():
    """Deliverable sections are written several ways in practice."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    for heading in (
        "## Deliverables",
        "### 交付物",
        "## Output files",
        "## 产出物",
        "**Deliverable:**",
    ):
        body = f"{heading}\n- reports/out.md\n"
        assert _declared_artifact_paths(body) == ["reports/out.md"], heading


def test_deliverables_section_ends_at_the_next_heading():
    """Paths under a later section are not pulled in."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    body = """
## Deliverables
- reports/wanted.md

## Notes
- reports/not_wanted.md
"""
    assert _declared_artifact_paths(body) == ["reports/wanted.md"]


def test_accepts_backticked_and_bare_paths():
    """Both `path` and bare path forms are picked up."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    body = "## Deliverables\n- `docs/a.md`\n- docs/b.md\n"
    assert _declared_artifact_paths(body) == ["docs/a.md", "docs/b.md"]


def test_empty_body_is_safe():
    """No body, no declarations, no crash."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    assert _declared_artifact_paths(None) == []
    assert _declared_artifact_paths("") == []


# ---------------------------------------------------------------------------
# Prose declarations
#
# Cards in the wild name their outputs mid-sentence rather than under a
# heading. The card that motivated this gate said "每轮评审结果写入
# /home/.../k4-uboot-fix-r<N>.md" in a numbered requirement, closed as
# done, and left nothing on disk.
# ---------------------------------------------------------------------------


def test_prose_write_verb_declares_an_artifact():
    """A path introduced by a write verb is an output, not a reference."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    body = "4. 每轮评审结果写入 /home/user/docs/reviews/round1.md\n"
    assert _declared_artifact_paths(body) == ["/home/user/docs/reviews/round1.md"]


def test_prose_english_write_verbs():
    """The same shape in English."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    for verb in ("write to", "save to", "output to", "write"):
        body = f"Please {verb} reports/out.md when finished.\n"
        assert _declared_artifact_paths(body) == ["reports/out.md"], verb


def test_prose_read_verbs_are_not_declarations():
    """Files the worker must consult are inputs and must not be required."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    for line in (
        "先读 docs/design/spec.md 再动手",
        "Read configs/imx8qxp_defconfig for current settings",
        "参考 tools/image-host.c 的实现",
        "评审范围包括 src/main.py",
    ):
        assert _declared_artifact_paths(line) == [], line


def test_prose_placeholder_paths_are_skipped():
    """A templated name cannot be checked, so it must not block."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    body = "每轮结果写入 docs/reviews/k4-uboot-fix-r<N>.md\n"
    assert _declared_artifact_paths(body) == []


def test_prose_and_section_declarations_combine_without_duplicates():
    from hermes_cli.kanban_db import _declared_artifact_paths

    body = """
## Deliverables
- reports/a.md

Also write reports/b.md, and write reports/a.md again.
"""
    assert _declared_artifact_paths(body) == ["reports/a.md", "reports/b.md"]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _make_task(workspace_path, body):
    """Build a minimal Task carrying just what the gate reads."""
    import time

    return kb.Task(
        id="t_test",
        title="Review: something",
        body=body,
        assignee="reviewer",
        status="running",
        priority=0,
        created_by="user",
        created_at=int(time.time()),
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=str(workspace_path),
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )


def test_gate_rejects_when_declared_file_is_absent(tmp_path):
    """The defect this exists for: card says done, disk says nothing."""
    from hermes_cli.kanban_db import _missing_artifact_rejection

    task = _make_task(tmp_path, "## Deliverables\n- reports/r1.md\n")
    rejection = _missing_artifact_rejection(task)

    assert rejection is not None
    assert "reports/r1.md" in rejection


def test_gate_passes_when_declared_file_exists(tmp_path):
    from hermes_cli.kanban_db import _missing_artifact_rejection

    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "r1.md").write_text("findings", encoding="utf-8")

    task = _make_task(tmp_path, "## Deliverables\n- reports/r1.md\n")
    assert _missing_artifact_rejection(task) is None


def test_gate_names_every_missing_file(tmp_path):
    """Reporting one at a time would mean three round trips."""
    from hermes_cli.kanban_db import _missing_artifact_rejection

    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "r1.md").write_text("x", encoding="utf-8")

    task = _make_task(
        tmp_path,
        "## Deliverables\n- reports/r1.md\n- reports/r2.md\n- reports/r3.md\n",
    )
    rejection = _missing_artifact_rejection(task)

    assert rejection is not None
    assert "reports/r2.md" in rejection
    assert "reports/r3.md" in rejection
    assert "reports/r1.md" not in rejection


def test_gate_is_silent_for_cards_declaring_nothing(tmp_path):
    """Most cards do not name deliverables; they must be unaffected."""
    from hermes_cli.kanban_db import _missing_artifact_rejection

    task = _make_task(tmp_path, "Fix the thing and commit it.")
    assert _missing_artifact_rejection(task) is None


def test_gate_is_silent_without_a_workspace(tmp_path):
    """A scratch card has nowhere to resolve relative paths against."""
    from hermes_cli.kanban_db import _missing_artifact_rejection

    task = _make_task(tmp_path, "## Deliverables\n- reports/r1.md\n")
    task.workspace_path = None
    assert _missing_artifact_rejection(task) is None


def test_gate_accepts_absolute_paths(tmp_path):
    """An absolute declaration is checked as given, not joined."""
    from hermes_cli.kanban_db import _missing_artifact_rejection

    target = tmp_path / "elsewhere" / "report.md"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")

    task = _make_task(tmp_path / "workspace", f"## Deliverables\n- {target}\n")
    assert _missing_artifact_rejection(task) is None


def test_gate_treats_an_empty_file_as_missing(tmp_path):
    """A zero-byte report is the same evidence as no report."""
    from hermes_cli.kanban_db import _missing_artifact_rejection

    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "r1.md").write_text("", encoding="utf-8")

    task = _make_task(tmp_path, "## Deliverables\n- reports/r1.md\n")
    rejection = _missing_artifact_rejection(task)

    assert rejection is not None
    assert "reports/r1.md" in rejection


def test_bold_note_inside_deliverables_does_not_terminate_section():
    """P2-2: Bold text like **Note:** inside deliverables does not end the section."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    body = """
## Deliverables
- reports/r1.md
**Note:** This is an important note
- reports/r2.md
"""
    assert _declared_artifact_paths(body) == ["reports/r1.md", "reports/r2.md"]


def test_placeholder_regex_does_not_flag_valid_filenames_with_n():
    """P2-3: Names like N-report.md or r1-N.md are valid files, not template placeholders."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    body = """
## Deliverables
- reports/N-report.md
- reports/r1-N.md
"""
    assert _declared_artifact_paths(body) == [
        "reports/N-report.md",
        "reports/r1-N.md",
    ]


def test_gate_rejects_path_traversal(tmp_path):
    """P2-4: A relative path traversing outside the workspace root is rejected."""
    from hermes_cli.kanban_db import _missing_artifact_rejection

    outside_file = tmp_path / "outside.md"
    outside_file.write_text("secret", encoding="utf-8")

    ws = tmp_path / "workspace"
    ws.mkdir()

    task = _make_task(ws, "## Deliverables\n- ../outside.md\n")
    rejection = _missing_artifact_rejection(task)

    assert rejection is not None
    assert "../outside.md" in rejection


def test_section_fallthrough_preserves_later_paths_after_non_path_line():
    """P2-5: Non-path lines (plain notes) inside deliverables do not discard later items."""
    from hermes_cli.kanban_db import _declared_artifact_paths

    body = """
## Deliverables
- reports/r1.md
some note without bold or heading
- reports/r2.md
"""
    assert _declared_artifact_paths(body) == ["reports/r1.md", "reports/r2.md"]


def test_rejection_message_does_not_leak_workspace_path(tmp_path):
    """P3-8: The error message lists missing files without leaking the full workspace root."""
    from hermes_cli.kanban_db import _missing_artifact_rejection

    ws = tmp_path / "secret_workspace_dir"
    task = _make_task(ws, "## Deliverables\n- reports/r1.md\n")
    rejection = _missing_artifact_rejection(task)

    assert rejection is not None
    assert "reports/r1.md" in rejection
    assert "secret_workspace_dir" not in rejection
    assert str(ws) not in rejection


def test_cmd_complete_artifact_gate_integration(kanban_home, tmp_path):
    """P3-9: Full CLI path: card with deliverables fails completion when absent, succeeds when present."""
    import argparse
    from hermes_cli import kanban as kc
    from hermes_cli import kanban_db as kb

    ws = tmp_path / "task_workspace"
    ws.mkdir()

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="Produce report",
            body="## Deliverables\n- final_report.md\n",
            workspace_kind="dir",
            workspace_path=str(ws),
        )

    # 1. File absent -> complete exits nonzero with rejection
    args = argparse.Namespace(task_ids=[tid], summary="Done", result=None, metadata=None)
    rc = kc._cmd_complete(args)
    assert rc != 0

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status != "done"

    # 2. Touch the file -> complete succeeds
    report_file = ws / "final_report.md"
    report_file.write_text("Report content", encoding="utf-8")

    rc = kc._cmd_complete(args)
    assert rc == 0

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "done"


def test_bold_deliverable_keyword_phrase_does_not_terminate_section():
    """P3-A: Bold phrases that merely start with a deliverable keyword must not
    terminate the section. Only an actual bold heading does.

    `**Deliverable text** is still being refined` is prose, not a heading --
    treating it as the section boundary drops every path after it.
    """
    from hermes_cli.kanban_db import _declared_artifact_paths

    body = """
## Deliverables
- reports/r1.md
**Deliverable text** is still being refined
- reports/r2.md
"""
    assert _declared_artifact_paths(body) == ["reports/r1.md", "reports/r2.md"]


