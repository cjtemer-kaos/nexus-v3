"""
Multi-File Editor — Atomic multi-file editing engine for SuperNEXUS v2.

Provides Cursor/Windsurf-like editing capabilities:
- Single-file edit with diff preview
- Patch parsing and application
- Atomic batch editing (all-or-nothing)
- In-memory edit history with undo
- Unified diff generation

Singleton: get_editor()
"""

import difflib
import logging
import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger("nexus-multi-editor")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EditResult:
    """Result of a single edit operation."""
    filepath: str
    success: bool
    old_text: str
    new_text: str
    diff: str = ""
    error: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "filepath": self.filepath,
            "success": self.success,
            "old_text": self.old_text,
            "new_text": self.new_text,
            "diff": self.diff,
            "error": self.error,
            "timestamp": self.timestamp,
        }


@dataclass
class EditEdit:
    """A single edit instruction within a batch."""
    filepath: str
    old_text: str
    new_text: str

    def to_dict(self) -> dict:
        return {
            "filepath": self.filepath,
            "old_text": self.old_text,
            "new_text": self.new_text,
        }


@dataclass
class EditBatch:
    """A collection of edits to be applied atomically."""
    edits: List[EditEdit] = field(default_factory=list)
    all_or_nothing: bool = True

    def to_dict(self) -> dict:
        return {
            "edits": [e.to_dict() for e in self.edits],
            "all_or_nothing": self.all_or_nothing,
        }


# ---------------------------------------------------------------------------
# Diff generation
# ---------------------------------------------------------------------------

def generate_unified_diff(
    filepath: str,
    old_text: str,
    new_text: str,
    n_lines: int = 3,
) -> str:
    """
    Produce a unified diff string for the given change.

    Returns a human-readable diff in unified format.
    """
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath}",
        n=n_lines,
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# Patch parser
# ---------------------------------------------------------------------------

class _PatchParser:
    """Parse a unified-diff-style patch string into EditEdit instructions."""

    @staticmethod
    def parse(patch_content: str) -> List[EditEdit]:
        """
        Parse patch content into individual file edits.

        Supports standard unified diff format:
            --- a/path/to/file
            +++ b/path/to/file
            @@ ... @@
            -old line
            +new line
        """
        edits: List[EditEdit] = []
        current_file: Optional[str] = None
        old_lines: List[str] = []
        new_lines: List[str] = []
        in_hunk = False

        for line in patch_content.splitlines():
            if line.startswith("--- a/"):
                # Save previous file if any
                if current_file and (old_lines or new_lines):
                    edits.append(EditEdit(
                        filepath=current_file,
                        old_text="\n".join(old_lines),
                        new_text="\n".join(new_lines),
                    ))
                current_file = line[6:]
                old_lines = []
                new_lines = []
                in_hunk = False
            elif line.startswith("+++ b/"):
                pass  # just the new-file header, file already set
            elif line.startswith("@@"):
                in_hunk = True
            elif in_hunk:
                if line.startswith("-") and not line.startswith("---"):
                    old_lines.append(line[1:])
                elif line.startswith("+") and not line.startswith("+++"):
                    new_lines.append(line[1:])
                elif line.startswith(" "):
                    # Context line — include in both for accurate matching
                    old_lines.append(line[1:])
                    new_lines.append(line[1:])
                elif line.startswith("\\"):
                    pass  # "\ No newline at end of file"
            elif current_file:
                # If no @@ header yet but we have a file, treat as a simple
                # line-level addition/removal
                if line.startswith("-"):
                    old_lines.append(line[1:])
                elif line.startswith("+"):
                    new_lines.append(line[1:])

        # Don't forget the last file
        if current_file and (old_lines or new_lines):
            edits.append(EditEdit(
                filepath=current_file,
                old_text="\n".join(old_lines),
                new_text="\n".join(new_lines),
            ))

        return edits


# ---------------------------------------------------------------------------
# Main editor
# ---------------------------------------------------------------------------

class MultiFileEditor:
    """
    Multi-file editor with atomic batch operations and undo support.

    Each file maintains an in-memory edit history (max 20 entries).
    Batch edits are atomic: if any edit fails, none are applied.
    """

    MAX_HISTORY = 20

    def __init__(self) -> None:
        self._history: Dict[str, Deque[EditResult]] = defaultdict(
            lambda: deque(maxlen=self.MAX_HISTORY)
        )
        self._lock = threading.Lock()
        logger.info("MultiFileEditor initialised")

    # ------------------------------------------------------------------
    # Single edit
    # ------------------------------------------------------------------

    def apply_edit(
        self, filepath: str, old_text: str, new_text: str
    ) -> EditResult:
        """
        Apply a single text replacement to *filepath*.

        If *old_text* is empty the file is created/overwritten with *new_text*.
        Returns an EditResult indicating success or failure.
        """
        # Validate
        if not old_text and not new_text:
            return EditResult(
                filepath=filepath, success=False, old_text=old_text,
                new_text=new_text, error="Both old_text and new_text are empty",
            )

        try:
            path = Path(filepath)
            path.parent.mkdir(parents=True, exist_ok=True)

            current_content = ""
            if path.exists():
                current_content = path.read_text(encoding="utf-8", errors="replace")

            if old_text:
                if old_text not in current_content:
                    return EditResult(
                        filepath=filepath, success=False,
                        old_text=old_text, new_text=new_text,
                        error="old_text not found in file",
                    )
                new_content = current_content.replace(old_text, new_text, 1)
            else:
                # Creating / overwriting
                new_content = new_text

            # Generate diff
            diff = generate_unified_diff(filepath, current_content, new_content)

            # Write
            path.write_text(new_content, encoding="utf-8")

            result = EditResult(
                filepath=filepath, success=True,
                old_text=old_text, new_text=new_text, diff=diff,
            )

            with self._lock:
                self._history[filepath].append(result)

            logger.debug("Edit applied to %s", filepath)
            return result

        except Exception as exc:
            return EditResult(
                filepath=filepath, success=False,
                old_text=old_text, new_text=new_text,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Patch application
    # ------------------------------------------------------------------

    def apply_patch(self, patch_content: str) -> List[EditResult]:
        """
        Parse a unified diff patch and apply all edits.

        Returns a list of EditResult, one per file.
        """
        edits = _PatchParser.parse(patch_content)
        if not edits:
            logger.warning("No edits found in patch")
            return []

        # Validate all edits before applying (atomic)
        results: List[EditResult] = []
        for edit in edits:
            result = self.apply_edit(edit.filepath, edit.old_text, edit.new_text)
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Batch editing (atomic)
    # ------------------------------------------------------------------

    def apply_batch(self, edits: List[EditEdit]) -> List[EditResult]:
        """
        Apply multiple edits atomically.

        If *all_or_nothing* is True (default), any failure rolls back all
        successful edits in this batch.
        """
        if not edits:
            return []

        # Phase 1: validate all edits (read file state without writing)
        validation: List[tuple] = []  # (filepath, current_content, old_text, new_text)
        for edit in edits:
            try:
                path = Path(edit.filepath)
                current = ""
                if path.exists():
                    current = path.read_text(encoding="utf-8", errors="replace")

                if edit.old_text and edit.old_text not in current:
                    # Atomic rollback: all fail
                    return [
                        EditResult(
                            filepath=e.filepath, success=False,
                            old_text=e.old_text, new_text=e.new_text,
                            error="old_text not found in file",
                        )
                        for e in edits
                    ]
                validation.append((edit.filepath, current, edit.old_text, edit.new_text))
            except Exception as exc:
                return [
                    EditResult(
                        filepath=e.filepath, success=False,
                        old_text=e.old_text, new_text=e.new_text,
                        error=str(exc),
                    )
                    for e in edits
                ]

        # Phase 2: apply all edits (chained — each edit applies to the
        # result of the previous one so multiple edits on the same file work)
        results: List[EditResult] = []
        applied: List[tuple] = []  # for rollback tracking
        # Track per-file running content so edits on the same file chain
        file_content: Dict[str, str] = {fp: cur for fp, cur, _, _ in validation}

        for (filepath, _original, old_text, new_text), edit in zip(validation, edits):
            try:
                current = file_content.get(filepath, _original)
                if old_text:
                    new_content = current.replace(old_text, new_text, 1)
                else:
                    new_content = new_text

                diff = generate_unified_diff(filepath, current, new_content)

                path = Path(filepath)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new_content, encoding="utf-8")

                # Update running content for subsequent edits on same file
                file_content[filepath] = new_content

                result = EditResult(
                    filepath=filepath, success=True,
                    old_text=old_text, new_text=new_text, diff=diff,
                )
                results.append(result)
                applied.append((filepath, _original))  # store original for rollback

            except Exception as exc:
                results.append(EditResult(
                    filepath=filepath, success=False,
                    old_text=old_text, new_text=new_text, error=str(exc),
                ))
                # Rollback all previously applied edits
                for rb_path, rb_content in reversed(applied):
                    try:
                        Path(rb_path).write_text(rb_content, encoding="utf-8")
                    except Exception:
                        logger.error("Rollback failed for %s", rb_path)
                # Mark remaining as failed
                for remaining_edit in edits[len(results):]:
                    results.append(EditResult(
                        filepath=remaining_edit.filepath, success=False,
                        old_text=remaining_edit.old_text,
                        new_text=remaining_edit.new_text,
                        error="Rolled back due to batch failure",
                    ))
                return results

        # All succeeded — record in history
        with self._lock:
            for r in results:
                if r.success:
                    self._history[r.filepath].append(r)

        return results

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def preview_edit(
        self, filepath: str, old_text: str, new_text: str
    ) -> str:
        """
        Preview what an edit would look like without writing.

        Returns a unified diff string.
        """
        try:
            path = Path(filepath)
            current = ""
            if path.exists():
                current = path.read_text(encoding="utf-8", errors="replace")

            if old_text and old_text not in current:
                return f"ERROR: old_text not found in {filepath}"

            if old_text:
                new_content = current.replace(old_text, new_text, 1)
            else:
                new_content = new_text

            return generate_unified_diff(filepath, current, new_content)

        except Exception as exc:
            return f"ERROR: {exc}"

    # ------------------------------------------------------------------
    # History & undo
    # ------------------------------------------------------------------

    def get_edit_history(self, filepath: str) -> List[EditResult]:
        """Return the edit history for *filepath* (most recent last)."""
        with self._lock:
            return list(self._history.get(filepath, []))

    def undo_last(self, filepath: str) -> Optional[EditResult]:
        """
        Undo the most recent edit for *filepath*.

        Restores the file to its state before that edit.
        Returns the undo result, or None if no history.
        """
        with self._lock:
            history = self._history.get(filepath)
            if not history:
                return None
            last = history[-1]

        if not last.success:
            return None

        # Apply reverse edit: swap new_text → old_text
        path = Path(filepath)
        if not path.exists():
            return EditResult(
                filepath=filepath, success=False,
                old_text=last.new_text, new_text=last.old_text,
                error="File no longer exists",
            )

        current = path.read_text(encoding="utf-8", errors="replace")
        if last.new_text and last.new_text not in current:
            return EditResult(
                filepath=filepath, success=False,
                old_text=last.new_text, new_text=last.old_text,
                error="Cannot find the previously written text to undo",
            )

        restored = current.replace(last.new_text, last.old_text, 1)
        diff = generate_unified_diff(filepath, current, restored)
        path.write_text(restored, encoding="utf-8")

        undo_result = EditResult(
            filepath=filepath, success=True,
            old_text=last.new_text, new_text=last.old_text, diff=diff,
        )

        with self._lock:
            self._history[filepath].append(undo_result)

        logger.debug("Undid last edit on %s", filepath)
        return undo_result

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_edit(self, filepath: str, old_text: str) -> bool:
        """
        Check whether *old_text* exists in the current content of *filepath*.

        Returns True if the text is found, False otherwise.
        """
        try:
            path = Path(filepath)
            if not path.exists():
                return False
            content = path.read_text(encoding="utf-8", errors="replace")
            return old_text in content
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def clear_history(self, filepath: Optional[str] = None) -> None:
        """Clear edit history for one file or all files."""
        with self._lock:
            if filepath:
                self._history.pop(filepath, None)
            else:
                self._history.clear()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[MultiFileEditor] = None
_instance_lock = threading.Lock()


def get_editor() -> MultiFileEditor:
    """Return the global MultiFileEditor singleton."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = MultiFileEditor()
    return _instance
