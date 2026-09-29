import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus-response")


@dataclass
class ResponseBlock:
    type: str
    content: str
    metadata: Dict = field(default_factory=dict)


class ResponseBuilder:
    def __init__(self, max_inline_chars: int = 50000):
        self._blocks: List[ResponseBlock] = []
        self._include_pages: bool = False
        self._include_snapshot: bool = False
        self._include_screenshot: bool = False
        self._include_console: bool = False
        self._include_network: bool = False
        self._include_trace: bool = False
        self._max_inline_chars = max_inline_chars
        self._pages_data: List[Dict] = []
        self._snapshot_text: str = ""
        self._screenshot_path: str = ""
        self._console_data: List[Dict] = []
        self._network_data: List[Dict] = []
        self._trace_summary: str = ""
        self._structured: List[Dict] = []
        self._start_time = time.time()

    def append_text(self, text: str):
        self._blocks.append(ResponseBlock("text", text))

    def append_line(self, line: str):
        self._blocks.append(ResponseBlock("text", line + "\n"))

    def append_heading(self, text: str, level: int = 2):
        prefix = "#" * level
        self._blocks.append(ResponseBlock("text", f"{prefix} {text}\n\n"))

    def append_code(self, code: str, language: str = ""):
        self._blocks.append(ResponseBlock("code", code, {"language": language}))

    def append_json(self, data: Any, label: str = ""):
        formatted = json.dumps(data, indent=2, default=str)
        if label:
            self.append_line(f"**{label}:**")
        self.append_code(formatted, "json")

    def append_error(self, message: str):
        self._blocks.append(ResponseBlock("error", message))

    _TEXT_EXTENSIONS = frozenset({".py", ".js", ".ts", ".json", ".md", ".txt", ".yml", ".yaml",
                                   ".html", ".css", ".xml", ".sh", ".bat", ".ps1", ".env", ".cfg",
                                   ".ini", ".toml", ".csv", ".log"})
    _MAX_FILE_SIZE = 10 * 1024 * 1024

    def append_file(self, path: str, label: str = ""):
        if not os.path.exists(path):
            return
        size = os.path.getsize(path)
        if size > self._MAX_FILE_SIZE:
            block = ResponseBlock("file_ref", path, {"path": path, "size": size, "truncated": True, "reason": "over_max_size"})
        elif os.path.splitext(path)[1].lower() not in self._TEXT_EXTENSIONS:
            block = ResponseBlock("file_ref", path, {"path": path, "size": size, "truncated": True, "reason": "binary"})
        elif size <= self._max_inline_chars:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            block = ResponseBlock("file", content, {"path": path, "size": size})
        else:
            block = ResponseBlock("file_ref", path, {"path": path, "size": size, "truncated": True})
        if label:
            block.metadata["label"] = label
        self._blocks.append(block)

    def append_image(self, image_data: str, mime: str = "image/png", alt: str = ""):
        self._blocks.append(ResponseBlock("image", image_data, {"mime": mime, "alt": alt}))

    def set_include_pages(self, enabled: bool):
        self._include_pages = enabled

    def set_include_snapshot(self, enabled: bool):
        self._include_snapshot = enabled

    def set_include_screenshot(self, enabled: bool):
        self._include_screenshot = enabled

    def set_include_console(self, enabled: bool):
        self._include_console = enabled

    def set_include_network(self, enabled: bool):
        self._include_network = enabled

    def set_include_trace(self, enabled: bool):
        self._include_trace = enabled

    def set_pages_data(self, pages: List[Dict]):
        self._pages_data = pages

    def set_snapshot_text(self, text: str):
        self._snapshot_text = text

    def set_screenshot_path(self, path: str):
        self._screenshot_path = path

    def set_console_data(self, data: List[Dict]):
        self._console_data = data

    def set_network_data(self, data: List[Dict]):
        self._network_data = data

    def set_trace_summary(self, summary: str):
        self._trace_summary = summary

    def add_structured(self, block_type: str, data: Any):
        self._structured.append({"type": block_type, "data": data})

    def _format_block_text(self, block: ResponseBlock) -> str:
        if block.type == "code":
            lang = block.metadata.get("language", "")
            return f"```{lang}\n{block.content}\n```\n"
        if block.type == "error":
            return f"[Error] {block.content}\n"
        if block.type == "file":
            label = block.metadata.get("label", "")
            prefix = f"**{label}:**\n" if label else ""
            return f"{prefix}{block.content}\n"
        return block.content

    def _build_text_content(self) -> str:
        parts = []
        if self._pages_data and self._include_pages:
            parts.append(f"**Pages ({len(self._pages_data)}):**\n")
            for p in self._pages_data:
                title = p.get("title", "(untitled)")
                url = p.get("url", "")
                pid = p.get("id", "?")
                parts.append(f"  [{pid}] {title} ({url})\n")

        if self._snapshot_text and self._include_snapshot:
            lines = self._snapshot_text.split("\n")
            max_snap = 60
            snap_text = "\n".join(lines[:max_snap])
            if len(lines) > max_snap:
                snap_text += f"\n... ({len(lines) - max_snap} more nodes)"
            parts.append(f"\n**Page Snapshot:**\n{snap_text}\n")

        if self._console_data and self._include_console:
            parts.append(f"\n**Console ({len(self._console_data)} messages):**\n")
            for msg in self._console_data[-5:]:
                parts.append(f"  {msg.get('level', 'log')}: {str(msg.get('text', ''))[:120]}\n")

        if self._trace_summary and self._include_trace:
            parts.append(f"\n**Performance:** {self._trace_summary}\n")

        for block in self._blocks:
            parts.append(self._format_block_text(block))

        return "".join(parts)

    def _format_block_structured(self, block: ResponseBlock) -> Optional[Dict]:
        if block.type == "text":
            return {"type": "text", "text": block.content}
        if block.type == "code":
            return {"type": "text", "text": f"```{block.metadata.get('language', '')}\n{block.content}\n```\n"}
        if block.type == "error":
            return {"type": "text", "text": f"Error: {block.content}"}
        if block.type == "image":
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": block.metadata.get("mime", "image/png"), "data": block.content},
                "alt": block.metadata.get("alt", "Screenshot"),
            }
        return None

    def _build_structured_content(self) -> List[Dict]:
        result = []

        for block in self._blocks:
            entry = self._format_block_structured(block)
            if entry:
                result.append(entry)

        if self._snapshot_text and self._include_snapshot:
            result.append({"type": "text", "text": f"Page Snapshot:\n{self._snapshot_text[:3000]}"})

        if self._structured:
            for item in self._structured:
                result.append({"type": "text", "text": json.dumps(item["data"], indent=2, default=str)})

        return result

    def build(self) -> Dict:
        content = self._build_text_content()
        structured = self._build_structured_content()

        result = {
            "content": content,
            "structured": structured,
            "duration_ms": (time.time() - self._start_time) * 1000,
            "block_count": len(self._blocks),
        }

        if self._screenshot_path and self._include_screenshot and os.path.exists(self._screenshot_path):
            with open(self._screenshot_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
            ext = os.path.splitext(self._screenshot_path)[1].lower()
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
            result["screenshot"] = {"data": img_b64, "mime": mime, "path": self._screenshot_path}

        return result

    def clear(self):
        self._blocks.clear()
        self._pages_data = []
        self._snapshot_text = ""
        self._screenshot_path = ""
        self._console_data = []
        self._network_data = []
        self._trace_summary = ""
        self._structured = []
        self._start_time = time.time()
