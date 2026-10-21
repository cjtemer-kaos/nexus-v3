"""Parser y aplicador de `apply_patch` con matching difuso.

Port de `sdk/packages/core/src/extensions/tools/executors/apply-patch-parser.ts`
y `apply-patch.ts` de Cline (commit e9dafbb, Apache-2.0).

Mejoras sobre el parser anterior de nexus-v3 (que solo aceptaba
`*** Update File:` con contexto exacto y unico):
- contexto difuso: normaliza puntuacion Unicode, recorta espacios finales y
  acepta similitud >= 0.66 (con fuzz auditado);
- `*** Add File:`, `*** Delete File:` y `*** Move to:`;
- multiples archivos y hunks, con cierre atomico;
- si un hunk no matchea, se reporta con contexto y NO se aplica nada;
- conserva el EOL propio de cada archivo (CRLF/LF) y usa el EOL de la
  plataforma para archivos nuevos, sin mezclar finales de linea.

El parser no toca disco: `compute_patch_changes()` recibe el contenido actual
de los archivos y devuelve los cambios; el caller decide como escribirlos.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping

PATCH_MARKERS = {
    "BEGIN": "*** Begin Patch",
    "END": "*** End Patch",
    "ADD": "*** Add File: ",
    "UPDATE": "*** Update File: ",
    "DELETE": "*** Delete File: ",
    "MOVE": "*** Move to: ",
    "SECTION": "@@",
    "END_FILE": "*** End of File",
}

BASH_WRAPPERS = ("%%bash", "apply_patch", "EOF", "```")

_PUNCTUATION_MAP = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2212": "-", "\u201C": '"', "\u201D": '"', "\u201E": '"', "\u00AB": '"',
    "\u00BB": '"', "\u2018": "'", "\u2019": "'", "\u201B": "'", "\u00A0": " ",
    "\u202F": " ",
}


class PatchActionType(str, Enum):
    ADD = "add"
    DELETE = "delete"
    UPDATE = "update"


class DiffError(Exception):
    """Error de parseo o aplicacion de un patch."""


@dataclass(slots=True)
class PatchChunk:
    orig_index: int
    del_lines: list[str] = field(default_factory=list)
    ins_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PatchAction:
    type: PatchActionType
    new_file: str | None = None
    chunks: list[PatchChunk] = field(default_factory=list)
    move_path: str | None = None


@dataclass(slots=True)
class PatchWarning:
    path: str
    chunk_index: int | None
    message: str
    context: str | None = None


@dataclass(slots=True)
class Patch:
    actions: dict[str, PatchAction] = field(default_factory=dict)
    warnings: list[PatchWarning] = field(default_factory=list)


@dataclass(slots=True)
class PatchFileChange:
    type: PatchActionType
    old_content: str | None = None
    new_content: str | None = None
    move_path: str | None = None


@dataclass(slots=True)
class PatchChanges:
    changes: dict[str, PatchFileChange]
    fuzz: int = 0


def canonicalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = "".join(_PUNCTUATION_MAP.get(char, char) for char in text)
    return text.replace("\\`", "`").replace("\\'", "'").replace('\\"', '"')


def detect_line_ending(content: str) -> str:
    return "\r\n" if "\r\n" in content else "\n"


def normalize_line_endings(text: str, eol: str) -> str:
    return re.sub(r"\r\n|\n", eol, text)


def normalize_new_file_line_endings(content: str, eol: str | None = None) -> str:
    target = os.linesep if eol is None else eol
    if target == "\r\n" and "\r" not in content:
        return content.replace("\n", "\r\n")
    return content


def split_patch_input_lines(text: str) -> list[str]:
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


def _is_wrapper_line(line: str) -> bool:
    if line.strip() == "":
        return False
    return any(line.startswith(wrapper) for wrapper in BASH_WRAPPERS)


def _trim_wrapper_lines(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and _is_wrapper_line(lines[start]):
        start += 1
    while end > start and _is_wrapper_line(lines[end - 1]):
        end -= 1
    return lines[start:end]


def normalize_patch_input(text: str) -> list[str]:
    raw = split_patch_input_lines(text or "")
    begin_index = next(
        (i for i, line in enumerate(raw) if line.startswith(PATCH_MARKERS["BEGIN"])),
        -1,
    )
    end_index = -1
    for i in range(len(raw) - 1, -1, -1):
        if raw[i].startswith(PATCH_MARKERS["END"]):
            end_index = i
            break

    if begin_index != -1 or end_index != -1:
        if begin_index == -1 or end_index == -1 or end_index < begin_index:
            raise DiffError(
                "Invalid patch text - incomplete sentinels. Try breaking it into smaller patches."
            )
        return raw[begin_index:end_index + 1]

    stripped = _trim_wrapper_lines(raw)
    while stripped and stripped[0] == "":
        stripped.pop(0)
    while stripped and stripped[-1] == "":
        stripped.pop()
    return [PATCH_MARKERS["BEGIN"], *stripped, PATCH_MARKERS["END"]]


def extract_files_for_operations(lines: Iterable[str], markers: Iterable[str]) -> list[str]:
    markers = tuple(markers)
    files: list[str] = []
    for line in lines:
        for marker in markers:
            if line.startswith(marker):
                path = line[len(marker):].strip()
                if path and path not in files:
                    files.append(path)
                break
    return files


def _line(lines: list[str], index: int) -> str | None:
    return lines[index] if 0 <= index < len(lines) else None


class PatchParser:
    def __init__(self, lines: list[str], current_files: Mapping[str, str]):
        self.lines = lines
        self.current_files = dict(current_files)
        self.patch = Patch()
        self.index = 0
        self.fuzz = 0
        self.current_path: str | None = None

    def parse(self) -> tuple[Patch, int]:
        if _line(self.lines, self.index) and self.lines[self.index].startswith(PATCH_MARKERS["BEGIN"]):
            self.index += 1

        while self.index < len(self.lines) and not self._is_end_marker():
            self._parse_next_action()

        return self.patch, self.fuzz

    def _is_end_marker(self) -> bool:
        line = _line(self.lines, self.index)
        return bool(line and line.startswith(PATCH_MARKERS["END"]))

    def _add_warning(self, warning: PatchWarning) -> None:
        self.patch.warnings.append(warning)

    def _parse_next_action(self) -> None:
        line = _line(self.lines, self.index)
        if line is None:
            raise DiffError("Unexpected end of patch")
        if line.startswith(PATCH_MARKERS["UPDATE"]):
            self._parse_update(line[len(PATCH_MARKERS["UPDATE"]):].strip())
            return
        if line.startswith(PATCH_MARKERS["DELETE"]):
            self._parse_delete(line[len(PATCH_MARKERS["DELETE"]):].strip())
            return
        if line.startswith(PATCH_MARKERS["ADD"]):
            self._parse_add(line[len(PATCH_MARKERS["ADD"]):].strip())
            return
        raise DiffError(f"Unknown line while parsing: {line}")

    def _check_duplicate(self, path: str, operation: str) -> None:
        if path in self.patch.actions:
            raise DiffError(f"Duplicate {operation} for file: {path}")

    def _parse_update(self, path: str) -> None:
        self._check_duplicate(path, "update")
        self.current_path = path
        self.index += 1

        move_path: str | None = None
        line = _line(self.lines, self.index)
        if line and line.startswith(PATCH_MARKERS["MOVE"]):
            move_path = line[len(PATCH_MARKERS["MOVE"]):].strip()
            self.index += 1

        if path not in self.current_files:
            raise DiffError(f"Update File Error: Missing File: {path}")

        action = self._parse_update_file(self.current_files[path], path)
        action.move_path = move_path
        self.patch.actions[path] = action
        self.current_path = None

    def _parse_update_file(self, text: str, path: str) -> PatchAction:
        action = PatchAction(type=PatchActionType.UPDATE)
        file_lines = text.split("\n")
        index = 0
        stop_markers = (
            PATCH_MARKERS["END"],
            PATCH_MARKERS["UPDATE"],
            PATCH_MARKERS["DELETE"],
            PATCH_MARKERS["ADD"],
            PATCH_MARKERS["END_FILE"],
        )

        while True:
            current = _line(self.lines, self.index)
            if current is None:
                break
            if any(current.startswith(marker.strip()) for marker in stop_markers):
                break

            def_str = current[3:] if current.startswith("@@ ") else None
            section_str = current if current == "@@" else None

            if def_str is not None or section_str is not None:
                self.index += 1
            elif index != 0:
                raise DiffError(f"Invalid Line:\n{current}")

            if def_str and def_str.strip():
                canon_def = canonicalize(def_str.strip())
                for i in range(index, len(file_lines)):
                    file_line = file_lines[i]
                    if file_line and (
                        canonicalize(file_line) == canon_def
                        or canonicalize(file_line.strip()) == canon_def
                    ):
                        index = i + 1
                        if (
                            canonicalize(file_line.strip()) == canon_def
                            and canonicalize(file_line) != canon_def
                        ):
                            self.fuzz += 1
                        break

            next_context, chunks, end_patch_index, eof = _peek(self.lines, self.index)
            new_index, fuzz, similarity = _find_context(
                file_lines, next_context, index, eof
            )

            if new_index == -1:
                context_text = "\n".join(next_context)
                self._add_warning(PatchWarning(
                    path=self.current_path or path,
                    chunk_index=len(action.chunks),
                    message=(
                        "Could not find matching context "
                        f"(similarity: {similarity:.2f}). Chunk skipped."
                    ),
                    context=(
                        context_text[:200] + "..."
                        if len(context_text) > 200
                        else context_text
                    ),
                ))
                self.index = end_patch_index
            else:
                self.fuzz += fuzz
                for chunk in chunks:
                    chunk.orig_index += new_index
                    action.chunks.append(chunk)
                index = new_index + len(next_context)
                self.index = end_patch_index

        return action

    def _parse_delete(self, path: str) -> None:
        self._check_duplicate(path, "delete")
        if path not in self.current_files:
            raise DiffError(f"Delete File Error: Missing File: {path}")
        self.patch.actions[path] = PatchAction(type=PatchActionType.DELETE)
        self.index += 1

    def _parse_add(self, path: str) -> None:
        self._check_duplicate(path, "add")
        if path in self.current_files:
            raise DiffError(f"Add File Error: File already exists: {path}")

        self.index += 1
        lines: list[str] = []
        stop_markers = (
            PATCH_MARKERS["END"],
            PATCH_MARKERS["UPDATE"],
            PATCH_MARKERS["DELETE"],
            PATCH_MARKERS["ADD"],
        )
        while self.index < len(self.lines):
            current = self.lines[self.index]
            if any(current.startswith(marker.strip()) for marker in stop_markers):
                break
            self.index += 1
            if not current.startswith("+"):
                raise DiffError(f"Invalid Add File line (missing '+'): {current}")
            lines.append(current[1:])

        self.patch.actions[path] = PatchAction(
            type=PatchActionType.ADD,
            new_file="\n".join(lines),
        )


def _calculate_similarity(first: str, second: str) -> float:
    longer, shorter = (first, second) if len(first) > len(second) else (second, first)
    if not longer:
        return 1.0
    return (len(longer) - _levenshtein_distance(shorter, longer)) / len(longer)


def _levenshtein_distance(first: str, second: str) -> int:
    rows, cols = len(second) + 1, len(first) + 1
    matrix = [0] * (rows * cols)

    def at(row: int, col: int) -> int:
        return matrix[row * cols + col]

    for i in range(len(second) + 1):
        matrix[i * cols] = i
    for j in range(len(first) + 1):
        matrix[j] = j

    for i in range(1, len(second) + 1):
        for j in range(1, len(first) + 1):
            if second[i - 1] == first[j - 1]:
                matrix[i * cols + j] = at(i - 1, j - 1)
            else:
                matrix[i * cols + j] = 1 + min(
                    at(i - 1, j - 1), at(i, j - 1), at(i - 1, j)
                )
    return at(len(second), len(first))


def _find_context(
    lines: list[str], context: list[str], start: int, eof: bool
) -> tuple[int, int, float]:
    if not context:
        return start, 0, 1.0

    best_similarity = 0.0
    canonical_context = canonicalize("\n".join(context))

    def find_core(start_index: int) -> tuple[int, int, float]:
        nonlocal best_similarity
        for i in range(start_index, len(lines)):
            segment = canonicalize("\n".join(lines[i:i + len(context)]))
            if segment == canonical_context:
                return i, 0, 1.0
            similarity = _calculate_similarity(segment, canonical_context)
            best_similarity = max(best_similarity, similarity)

        trimmed_context = canonicalize("\n".join(line.rstrip() for line in context))
        for i in range(start_index, len(lines)):
            segment = canonicalize(
                "\n".join(line.rstrip() for line in lines[i:i + len(context)])
            )
            if segment == trimmed_context:
                return i, 1, 1.0

        stripped_context = canonicalize("\n".join(line.strip() for line in context))
        for i in range(start_index, len(lines)):
            segment = canonicalize(
                "\n".join(line.strip() for line in lines[i:i + len(context)])
            )
            if segment == stripped_context:
                return i, 100, 1.0

        threshold = 0.66
        for i in range(start_index, len(lines)):
            segment = canonicalize("\n".join(lines[i:i + len(context)]))
            similarity = _calculate_similarity(segment, canonical_context)
            if similarity >= threshold:
                return i, 1000, similarity
            best_similarity = max(best_similarity, similarity)

        return -1, 0, best_similarity

    if eof:
        # Un archivo terminado en "\n" produce un ultimo elemento vacio al
        # hacer split; sin descontarlo, el ancla End of File queda corrida una
        # linea y el fallback termina matcheando la primera ocurrencia.
        trailing_empty = 1 if lines and lines[-1] == "" else 0
        new_index, fuzz, similarity = find_core(
            max(0, len(lines) - len(context) - trailing_empty)
        )
        if new_index != -1:
            return new_index, fuzz, similarity
        new_index, fuzz, similarity = find_core(start)
        return new_index, fuzz + 10000, similarity

    return find_core(start)


def _peek(lines: list[str], initial_index: int) -> tuple[list[str], list[PatchChunk], int, bool]:
    index = initial_index
    old: list[str] = []
    del_lines: list[str] = []
    ins_lines: list[str] = []
    chunks: list[PatchChunk] = []
    mode = "keep"

    stop_markers = (
        "@@",
        PATCH_MARKERS["END"],
        PATCH_MARKERS["UPDATE"],
        PATCH_MARKERS["DELETE"],
        PATCH_MARKERS["ADD"],
        PATCH_MARKERS["END_FILE"],
    )

    while index < len(lines):
        source_line = lines[index]
        if not source_line or any(source_line.startswith(marker.strip()) for marker in stop_markers):
            break
        if source_line == "***":
            break
        if source_line.startswith("***"):
            raise DiffError(f"Invalid line: {source_line}")

        index += 1
        previous_mode = mode
        line = source_line

        if line.startswith("+"):
            mode = "add"
        elif line.startswith("-"):
            mode = "delete"
        elif line.startswith(" "):
            mode = "keep"
        else:
            mode = "keep"
            line = " " + line

        line = line[1:]

        if mode == "keep" and previous_mode != mode:
            if ins_lines or del_lines:
                chunks.append(PatchChunk(
                    orig_index=len(old) - len(del_lines),
                    del_lines=del_lines,
                    ins_lines=ins_lines,
                ))
            del_lines = []
            ins_lines = []

        if mode == "delete":
            del_lines.append(line)
            old.append(line)
        elif mode == "add":
            ins_lines.append(line)
        else:
            old.append(line)

    if ins_lines or del_lines:
        chunks.append(PatchChunk(
            orig_index=len(old) - len(del_lines),
            del_lines=del_lines,
            ins_lines=ins_lines,
        ))

    if index < len(lines) and lines[index] == PATCH_MARKERS["END_FILE"]:
        index += 1
        return old, chunks, index, True

    return old, chunks, index, False


def apply_chunks(content: str, chunks: list[PatchChunk], file_path: str) -> str:
    if not chunks:
        return content

    lines = content.split("\n")
    result: list[str] = []
    current_index = 0

    for chunk in chunks:
        if chunk.orig_index > len(lines):
            raise DiffError(
                f"{file_path}: chunk.origIndex {chunk.orig_index} > lines.length {len(lines)}"
            )
        if current_index > chunk.orig_index:
            raise DiffError(
                f"{file_path}: currentIndex {current_index} > chunk.origIndex {chunk.orig_index}"
            )
        result.extend(lines[current_index:chunk.orig_index])
        result.extend(chunk.ins_lines)
        current_index = chunk.orig_index + len(chunk.del_lines)

    result.extend(lines[current_index:])
    return "\n".join(result)


def _format_skipped_hunk_failure(warnings: list[PatchWarning]) -> str:
    lines = [
        f"Patch could not be applied because {len(warnings)} hunk"
        f"{'' if len(warnings) == 1 else 's'} did not match the current file content."
    ]
    for warning in warnings:
        hunk_number = "unknown" if warning.chunk_index is None else str(warning.chunk_index + 1)
        lines.append(f"{warning.path}: hunk {hunk_number}: {warning.message}")
        if warning.context:
            lines.append(f"Context:\n{warning.context}")
    return "\n".join(lines)


def compute_patch_changes(
    patch_text: str,
    current_files: Mapping[str, str],
    *,
    new_file_eol: str | None = None,
) -> PatchChanges:
    """Calcula los cambios sin escribir. `current_files` debe traer el contenido
    actual de los archivos referenciados por UPDATE/DELETE (y de los ADD que ya
    existan, para que el parser rechace el add)."""
    lines = normalize_patch_input(patch_text)
    files_for_ops = extract_files_for_operations(
        lines, (PATCH_MARKERS["UPDATE"], PATCH_MARKERS["DELETE"])
    )
    files: dict[str, str] = {}
    eols: dict[str, str] = {}
    for path in files_for_ops:
        if path not in current_files:
            raise DiffError(f"File not found: {path}")
        raw = current_files[path]
        files[path] = raw.replace("\r\n", "\n")
        eols[path] = detect_line_ending(raw)

    for path in extract_files_for_operations(lines, (PATCH_MARKERS["ADD"],)):
        if path in current_files:
            files[path] = current_files[path]

    parser = PatchParser(lines, files)
    patch, fuzz = parser.parse()
    if patch.warnings:
        raise DiffError(_format_skipped_hunk_failure(patch.warnings))

    def with_eol(path: str, content: str | None) -> str | None:
        if content is None:
            return None
        return normalize_line_endings(content, eols.get(path, "\n"))

    changes: dict[str, PatchFileChange] = {}
    for path, action in patch.actions.items():
        if action.type is PatchActionType.DELETE:
            changes[path] = PatchFileChange(
                type=PatchActionType.DELETE,
                old_content=with_eol(path, files.get(path)),
            )
        elif action.type is PatchActionType.ADD:
            if action.new_file is None:
                raise DiffError("ADD action without file content")
            changes[path] = PatchFileChange(
                type=PatchActionType.ADD,
                new_content=normalize_new_file_line_endings(action.new_file, new_file_eol),
            )
        else:
            original = files.get(path, "")
            changes[path] = PatchFileChange(
                type=PatchActionType.UPDATE,
                old_content=with_eol(path, original),
                new_content=with_eol(path, apply_chunks(original, action.chunks, path)),
                move_path=action.move_path,
            )

    return PatchChanges(changes=changes, fuzz=fuzz)


def patch_operation_paths(patch_text: str) -> list[tuple[PatchActionType, str]]:
    """Paths y operacion declarados, para validar scope antes de leer/escribir."""
    lines = normalize_patch_input(patch_text)
    out: list[tuple[PatchActionType, str]] = []
    for path in extract_files_for_operations(lines, (PATCH_MARKERS["UPDATE"],)):
        out.append((PatchActionType.UPDATE, path))
    for path in extract_files_for_operations(lines, (PATCH_MARKERS["DELETE"],)):
        out.append((PatchActionType.DELETE, path))
    for path in extract_files_for_operations(lines, (PATCH_MARKERS["ADD"],)):
        out.append((PatchActionType.ADD, path))
    return out
