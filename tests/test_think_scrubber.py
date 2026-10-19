"""Tests del StreamingThinkScrubber: supresion de razonamiento en streaming."""
from src.core.think_scrubber import StreamingThinkScrubber


def _run(chunks):
    s = StreamingThinkScrubber()
    out = []
    for c in chunks:
        visible = s.feed(c)
        if visible:
            out.append(visible)
    tail = s.flush()
    if tail:
        out.append(tail)
    return "".join(out)


def test_plain_text_passes_through():
    assert _run(["hola"]) == "hola"
    assert _run(["a", "b", "c"]) == "abc"


def test_single_closed_block_removed():
    assert _run(["<thinking>secreto</thinking>visible"]) == "visible"


def test_all_supported_tag_names():
    for tag in ("think", "thinking", "reasoning", "thought", "REASONING_SCRATCHPAD"):
        assert _run([f"<{tag}>oculto</{tag}>ok"]) == "ok"


def test_case_insensitive_tags():
    assert _run(["<THINKING>x</THINKING>ok"]) == "ok"


def test_inline_closed_block_removed():
    assert _run(["antes <reasoning>x</reasoning> despues"]) == "antes  despues"


def test_partial_tag_held_across_deltas():
    assert _run(["texto\n<th", "ink>oculto</thinking>fin"]) == "texto\nfin"


def test_stream_splits_inside_closing_tag():
    assert _run(["<think>oculto</thi", "nk>visible"]) == "visible"


def test_unclosed_block_swallows_rest():
    out = _run(["a\n<thinking>oculto"])
    assert out == "a\n"


def test_orphan_close_tag_stripped():
    assert _run(["a</thinking>b"]) == "ab"


def test_inline_open_tag_without_close_is_not_a_block():
    assert _run(["usa <think> en html"]) == "usa <think> en html"


def test_multiline_content():
    assert _run(["uno\ndos\n<reasoning>no</reasoning>\ntres"]) == "uno\ndos\n\ntres"


def test_flush_returns_held_text_when_not_in_block():
    s = StreamingThinkScrubber()
    assert s.feed("texto\n<th") == "texto\n"
    assert s.flush() == "<th"


def test_reset_clears_state():
    s = StreamingThinkScrubber()
    s.feed("<thinking>resto")
    s.reset()
    assert s.feed("normal") == "normal"
