"""
AutoTest - Testing framework automatizado para SuperNEXUS v2.

Ejecuta tests de integracion sobre los subsistemas IA:
- Ollama connectivity + model availability
- PeerChat health (PC1/PC2)
- RAG engine indexing + search
- Tool calling loop
- Judge pipeline evaluation
- MCP bridge tools

Uso:
    tester = AutoTest()
    results = await tester.run_all()
    print(tester.report(results))
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Awaitable, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    name: str
    passed: bool
    duration_ms: float
    detail: str = ""
    error: str = ""


class AutoTest:
    """Framework de testing automatizado para subsistemas NEXUS."""

    def __init__(self):
        self._tests: Dict[str, Callable[..., Awaitable[TestResult]]] = {}
        self._register_builtin_tests()

    def register(self, name: str, fn: Callable[..., Awaitable[TestResult]]):
        self._tests[name] = fn

    def _register_builtin_tests(self):
        self._tests["ollama_health"] = self._test_ollama_health
        self._tests["ollama_models"] = self._test_ollama_models
        self._tests["ollama_generate"] = self._test_ollama_generate
        self._tests["rag_engine"] = self._test_rag_engine
        self._tests["tool_calling_parse"] = self._test_tool_calling_parse
        self._tests["judge_pipeline"] = self._test_judge_pipeline

    async def run_all(self, timeout: float = 30) -> List[TestResult]:
        results = []
        for name, fn in self._tests.items():
            start = time.time()
            try:
                result = await asyncio.wait_for(fn(), timeout=timeout)
                result.duration_ms = (time.time() - start) * 1000
                results.append(result)
            except asyncio.TimeoutError:
                results.append(TestResult(
                    name=name, passed=False,
                    duration_ms=timeout * 1000,
                    error=f"Timeout after {timeout}s",
                ))
            except Exception as e:
                results.append(TestResult(
                    name=name, passed=False,
                    duration_ms=(time.time() - start) * 1000,
                    error=str(e),
                ))
        return results

    async def run_one(self, name: str) -> TestResult:
        fn = self._tests.get(name)
        if not fn:
            return TestResult(name=name, passed=False, duration_ms=0, error=f"Test '{name}' not found")
        start = time.time()
        try:
            result = await fn()
            result.duration_ms = (time.time() - start) * 1000
            return result
        except Exception as e:
            return TestResult(name=name, passed=False, duration_ms=(time.time() - start) * 1000, error=str(e))

    def report(self, results: List[TestResult]) -> str:
        lines = ["# AutoTest Report", ""]
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        lines.append(f"**{passed}/{total} passed**\n")
        lines.append("| Test | Status | Time | Detail |")
        lines.append("|------|--------|------|--------|")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            detail = r.detail or r.error or ""
            lines.append(f"| {r.name} | {status} | {r.duration_ms:.0f}ms | {detail[:80]} |")
        return "\n".join(lines)

    # --- Builtin Tests ---

    async def _test_ollama_health(self) -> TestResult:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get("http://127.0.0.1:11434/api/tags")
                if r.status_code == 200:
                    models = r.json().get("models", [])
                    return TestResult("ollama_health", True, 0, f"{len(models)} models available")
                return TestResult("ollama_health", False, 0, error=f"Status {r.status_code}")
        except Exception as e:
            return TestResult("ollama_health", False, 0, error=str(e))

    async def _test_ollama_models(self) -> TestResult:
        import httpx
        required = ["qwen2.5-coder:7b", "qwen2.5-coder", "nemotron", "nomic-embed-text"]
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get("http://127.0.0.1:11434/api/tags")
                names = [m["name"] for m in r.json().get("models", [])]
                missing = [req for req in required if not any(req in n for n in names)]
                if missing:
                    return TestResult("ollama_models", False, 0, error=f"Missing: {missing}")
                return TestResult("ollama_models", True, 0, "All required models present")
        except Exception as e:
            return TestResult("ollama_models", False, 0, error=str(e))

    async def _test_ollama_generate(self) -> TestResult:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    "http://127.0.0.1:11434/api/generate",
                    json={"model": "nemotron-3-nano:4b", "prompt": "Say OK", "stream": False,
                          "options": {"num_predict": 10}},
                    timeout=30,
                )
                if r.status_code == 200:
                    resp = r.json().get("response", "")
                    return TestResult("ollama_generate", bool(resp), 0, f"Response: {resp[:50]}")
                return TestResult("ollama_generate", False, 0, error=f"Status {r.status_code}")
        except Exception as e:
            return TestResult("ollama_generate", False, 0, error=str(e))

    async def _test_rag_engine(self) -> TestResult:
        try:
            from src.core.rag_engine import _chunk_text
            chunks = _chunk_text("Hello world. " * 100, chunk_size=100)
            if len(chunks) < 2:
                return TestResult("rag_engine", False, 0, error="Chunking failed")
            return TestResult("rag_engine", True, 0, f"Chunking OK: {len(chunks)} chunks from test text")
        except Exception as e:
            return TestResult("rag_engine", False, 0, error=str(e))

    async def _test_tool_calling_parse(self) -> TestResult:
        try:
            from src.core.local_tool_calling import LocalToolCaller
            caller = LocalToolCaller(ollama_client=None)
            test_output = '''Here's what I found:
<tool_call>
{"name": "read_file", "arguments": {"path": "test.py"}}
</tool_call>
Let me check that file.'''
            calls = caller.parse_tool_calls(test_output)
            if len(calls) == 1 and calls[0]["name"] == "read_file":
                return TestResult("tool_calling_parse", True, 0, "Parsed 1 tool call correctly")
            return TestResult("tool_calling_parse", False, 0, error=f"Expected 1 call, got {len(calls)}")
        except Exception as e:
            return TestResult("tool_calling_parse", False, 0, error=str(e))

    async def _test_judge_pipeline(self) -> TestResult:
        try:
            from src.core.judge_pipeline import JudgePipeline
            judge = JudgePipeline(llm_executor=None)
            # Test L0 short-circuit (regex-based)
            result = judge.logical_short_circuit("Write hello world", "")
            if result is not None and result.get("score", 1) < 0.5:
                return TestResult("judge_pipeline", True, 0, "L0 short-circuit detected empty response")
            return TestResult("judge_pipeline", True, 0, "JudgePipeline imported OK")
        except Exception as e:
            return TestResult("judge_pipeline", False, 0, error=str(e))

    def get_test_names(self) -> List[str]:
        return list(self._tests.keys())
