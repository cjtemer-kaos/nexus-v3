"""Port de apply_patch (Cline): matching difuso, multi-archivo y EOL."""
from __future__ import annotations

import pytest

from src.core.apply_patch import (
    DiffError,
    PatchActionType,
    compute_patch_changes,
    normalize_patch_input,
    patch_operation_paths,
)


def _patch(*lines: str) -> str:
    return "\n".join(("*** Begin Patch", *lines, "*** End Patch"))


def test_exact_update_applies_and_reports_no_fuzz():
    patch = _patch(
        "*** Update File: a.txt",
        "@@",
        " line1",
        "-line2",
        "+LINE2",
        " line3",
    )
    result = compute_patch_changes(patch, {"a.txt": "line1\nline2\nline3"})
    change = result.changes["a.txt"]
    assert change.type is PatchActionType.UPDATE
    assert change.new_content == "line1\nLINE2\nline3"
    assert result.fuzz == 0


def test_canonicalizes_unicode_punctuation_before_matching():
    file_text = 'const x = "hola";\nconst y = 2;'
    patch = _patch(
        "*** Update File: a.ts",
        "@@",
        " const x = \u201Chola\u201D;",
        "-const y = 2;",
        "+const y = 3;",
    )
    result = compute_patch_changes(patch, {"a.ts": file_text})
    assert result.changes["a.ts"].new_content == 'const x = "hola";\nconst y = 3;'


def test_tolerates_trailing_whitespace_with_fuzz():
    patch = _patch(
        "*** Update File: a.txt",
        "@@",
        " line1",
        "-line2",
        "+LINE2",
    )
    result = compute_patch_changes(patch, {"a.txt": "line1   \nline2\n"})
    assert "LINE2" in result.changes["a.txt"].new_content
    assert result.fuzz >= 1


def test_accepts_similar_context_above_threshold():
    patch = _patch(
        "*** Update File: a.txt",
        "@@",
        " abc def gho",
        "-second",
        "+SECOND",
    )
    result = compute_patch_changes(patch, {"a.txt": "abc def ghi\nsecond\n"})
    assert result.changes["a.txt"].new_content == "abc def ghi\nSECOND\n"
    assert result.fuzz >= 1000


def test_failed_hunk_reports_context_and_applies_nothing():
    patch = _patch(
        "*** Update File: a.txt",
        "@@",
        " totally different",
        "-x",
        "+y",
    )
    with pytest.raises(DiffError) as exc:
        compute_patch_changes(patch, {"a.txt": "line1\nline2\n"})
    text = str(exc.value)
    assert "hunk 1" in text
    assert "could not be applied" in text.lower()
    assert "totally different" in text


def test_add_file_uses_target_eol():
    patch = _patch("*** Add File: new.txt", "+hello", "+world")
    result = compute_patch_changes(patch, {}, new_file_eol="\r\n")
    change = result.changes["new.txt"]
    assert change.type is PatchActionType.ADD
    assert change.new_content == "hello\r\nworld"


def test_add_existing_file_is_rejected():
    patch = _patch("*** Add File: a.txt", "+hello")
    with pytest.raises(DiffError, match="already exists"):
        compute_patch_changes(patch, {"a.txt": "old"})


def test_delete_file_is_reported():
    patch = _patch("*** Delete File: gone.txt")
    result = compute_patch_changes(patch, {"gone.txt": "bye\n"})
    change = result.changes["gone.txt"]
    assert change.type is PatchActionType.DELETE
    assert change.old_content == "bye\n"


def test_move_file_is_reported_with_new_content():
    patch = _patch(
        "*** Update File: a.txt",
        "*** Move to: sub/b.txt",
        "@@",
        "-old",
        "+new",
    )
    result = compute_patch_changes(patch, {"a.txt": "old\n"})
    change = result.changes["a.txt"]
    assert change.move_path == "sub/b.txt"
    assert change.new_content == "new\n"


def test_multiple_files_and_disjoint_chunks():
    patch = _patch(
        "*** Update File: a.txt",
        "@@",
        "-a1",
        "+A1",
        "@@",
        "-a3",
        "+A3",
        "*** Update File: b.txt",
        "@@",
        "-b1",
        "+B1",
    )
    result = compute_patch_changes(
        patch,
        {"a.txt": "a1\na2\na3\n", "b.txt": "b1\nb2\n"},
    )
    assert result.changes["a.txt"].new_content == "A1\na2\nA3\n"
    assert result.changes["b.txt"].new_content == "B1\nb2\n"


def test_preserves_crlf_file_eol():
    patch = _patch(
        "*** Update File: win.txt",
        "@@",
        " first",
        "-second",
        "+SECOND",
    )
    result = compute_patch_changes(patch, {"win.txt": "first\r\nsecond\r\n"})
    assert result.changes["win.txt"].new_content == "first\r\nSECOND\r\n"


def test_end_of_file_anchor_prefers_tail_context():
    patch = _patch(
        "*** Update File: a.txt",
        "@@",
        " middle",
        "-last",
        "+LAST",
        "*** End of File",
    )
    result = compute_patch_changes(patch, {"a.txt": "last\nmiddle\nlast\n"})
    assert result.changes["a.txt"].new_content == "last\nmiddle\nLAST\n"


def test_accepts_bash_wrapper_around_patch():
    text = "%%bash\n" + _patch(
        "*** Update File: a.txt",
        "@@",
        "-old",
        "+new",
    ) + "\nEOF"
    lines = normalize_patch_input(text)
    assert lines[0] == "*** Begin Patch"
    assert lines[-1] == "*** End Patch"
    result = compute_patch_changes(text, {"a.txt": "old\n"})
    assert result.changes["a.txt"].new_content == "new\n"


def test_incomplete_sentinels_are_rejected():
    with pytest.raises(DiffError, match="incomplete sentinels"):
        normalize_patch_input("*** Begin Patch\n*** Update File: a.txt\n")


def test_patch_operation_paths_exposes_every_target_for_scope_checks():
    patch = _patch(
        "*** Update File: a.txt",
        "@@",
        "-x",
        "+y",
        "*** Add File: b.txt",
        "+b",
        "*** Delete File: c.txt",
    )
    ops = dict((path, op) for op, path in patch_operation_paths(patch))
    assert ops["a.txt"] is PatchActionType.UPDATE
    assert ops["b.txt"] is PatchActionType.ADD
    assert ops["c.txt"] is PatchActionType.DELETE
