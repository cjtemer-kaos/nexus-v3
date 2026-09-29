"""
ErrorCompactor - Compactación de errores para contexto

Compact errors into context window.

- Extrae última línea del traceback (la más útil)
- Extrae tipo de error y mensaje
- Head+tail pattern para outputs largos
"""

import logging
import re

logger = logging.getLogger("nexus-error")


class ErrorCompactor:
    MAX_ERROR_TOKENS = 200
    MAX_OUTPUT_LINES = 20

    def compact(self, error: str) -> str:
        """Reduce error a máximo ~200 tokens manteniendo info útil."""
        if not error:
            return "(empty error)"

        lines = error.strip().split("\n")

        if len(lines) <= 3:
            return error.strip()[:800]

        error_type = self._extract_error_type(error)
        last_line = lines[-1].strip() if lines else ""
        file_line = self._extract_file_line(error)

        parts = []
        if error_type:
            parts.append(f"Error type: {error_type}")
        if file_line:
            parts.append(f"Location: {file_line}")
        if last_line and last_line != error_type:
            parts.append(f"Message: {last_line[:300]}")

        if not parts:
            parts.append(f"Error: {lines[-1][:300]}")

        compacted = " | ".join(parts)

        if len(compacted) > self.MAX_ERROR_TOKENS * 4:
            compacted = compacted[:self.MAX_ERROR_TOKENS * 4] + "..."

        return compacted

    def compact_tool_result(self, output: str, max_lines: int = None) -> str:
        """Head + tail pattern para outputs de tools."""
        if max_lines is None:
            max_lines = self.MAX_OUTPUT_LINES

        lines = output.split("\n")
        if len(lines) <= max_lines:
            return output

        head_n = max_lines // 2
        tail_n = max_lines - head_n
        omitted = len(lines) - max_lines

        head = "\n".join(lines[:head_n])
        tail = "\n".join(lines[-tail_n:])

        return f"{head}\n... ({omitted} lines omitted) ...\n{tail}"

    def _extract_error_type(self, error: str) -> str:
        match = re.search(r'(\w+Error|\w+Exception):\s*(.*)', error)
        if match:
            return f"{match.group(1)}: {match.group(2)[:100]}"
        return ""

    def _extract_file_line(self, error: str) -> str:
        matches = re.findall(r'File "([^"]+)", line (\d+)', error)
        if matches:
            last_match = matches[-1]
            return f"{last_match[0]}:{last_match[1]}"
        match = re.search(r'at\s+(\S+)\s+\(([^:]+):(\d+)\)', error)
        if match:
            return f"{match.group(2)}:{match.group(3)}"
        return ""

    def compact_exception(self, exc: Exception) -> str:
        """Compacta una excepción Python directamente."""
        import traceback
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        full = "".join(tb)
        return self.compact(full)
