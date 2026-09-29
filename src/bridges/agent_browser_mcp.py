"""
agent-browser MCP Server - Browser automation for AI agents.

Wraps the agent-browser CLI (https://agent-browser.dev) as MCP tools.
Compact text output uses fewer tokens than raw DOM/JSON.

Usage:
    python agent_browser_mcp.py        # stdio mode (for MCP clients)
"""

import logging
import subprocess

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("agent-browser-mcp")

mcp = FastMCP("agent-browser")


def _run(args: list[str], timeout: int = 30) -> str:
    """Run an agent-browser command and return stdout."""
    cmd = ["agent-browser"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return f"[error] {r.stderr.strip() or r.stdout.strip()}"
        return r.stdout.strip() or "(empty)"
    except subprocess.TimeoutExpired:
        return "[timeout]"
    except FileNotFoundError:
        return "[error] agent-browser not installed. Run: npm install -g agent-browser"
    except Exception as e:
        return f"[error] {e}"


@mcp.tool()
async def browser_open(url: str = "") -> str:
    """Open a URL (or launch browser without nav). Use this first. Returns 'opened' on success."""
    args = ["open", url] if url else ["open"]
    return _run(args)


@mcp.tool()
async def browser_snapshot(interactive_only: bool = True) -> str:
    """Get accessibility tree with element refs (@e1, @e2, ...). Use after open() to discover elements. interactive_only=True returns only clickable/fillable elements."""
    args = ["snapshot"]
    if interactive_only:
        args.append("-i")
    return _run(args)


@mcp.tool()
async def browser_click(selector: str) -> str:
    """Click an element by ref (@e1), CSS selector (#btn), or role (button). Use refs from snapshot()."""
    return _run(["click", selector])


@mcp.tool()
async def browser_fill(selector: str, text: str) -> str:
    """Clear and fill an input field by ref (@e3) or CSS selector."""
    return _run(["fill", selector, text])


@mcp.tool()
async def browser_type(selector: str, text: str) -> str:
    """Type into an element (doesn't clear first)."""
    return _run(["type", selector, text])


@mcp.tool()
async def browser_press(key: str) -> str:
    """Press a keyboard key (Enter, Tab, Escape, Control+a, etc.)."""
    return _run(["press", key])


@mcp.tool()
async def browser_select(selector: str, value: str) -> str:
    """Select an option in a dropdown."""
    return _run(["select", selector, value])


@mcp.tool()
async def browser_hover(selector: str) -> str:
    """Hover over an element."""
    return _run(["hover", selector])


@mcp.tool()
async def browser_get_text(selector: str = "") -> str:
    """Get text content of the page or an element. Omit selector for full page text."""
    if selector:
        return _run(["get", "text", selector])
    return _run(["eval", "document.body.innerText"])


@mcp.tool()
async def browser_get_url() -> str:
    """Get the current page URL."""
    return _run(["get", "url"])


@mcp.tool()
async def browser_get_title() -> str:
    """Get the current page title."""
    return _run(["get", "title"])


@mcp.tool()
async def browser_eval(js: str) -> str:
    """Execute JavaScript in the page. Returns the result as string."""
    return _run(["eval", js])


@mcp.tool()
async def browser_screenshot(path: str = "") -> str:
    """Take a screenshot. Returns the file path. Use --full for full page capture."""
    args = ["screenshot"]
    if path:
        args.append(path)
    return _run(args)


@mcp.tool()
async def browser_wait(condition: str, value: str = "") -> str:
    """Wait for a condition. condition: selector (CSS), ms (milliseconds), text, url, load. For selector wait, value is the selector. For text wait, value is the text to wait for."""
    if condition == "selector":
        return _run(["wait", value])
    elif condition == "ms":
        return _run(["wait", value])
    elif condition == "text":
        return _run(["wait", "--text", value])
    elif condition == "url":
        return _run(["wait", "--url", value])
    elif condition == "load":
        return _run(["wait", "--load", value or "networkidle"])
    return _run(["wait", condition])


@mcp.tool()
async def browser_close() -> str:
    """Close the browser and end the session."""
    return _run(["close"])


@mcp.tool()
async def browser_navigate_and_extract(url: str) -> str:
    """One-shot: navigate to a URL, wait for load, and extract all page text. Best for content research (YouTube channels, docs, articles)."""
    out = _run(["open", url])
    if "[error]" in out:
        return out
    _run(["wait", "--load", "networkidle"])
    text = _run(["eval", "document.body.innerText"])
    title = _run(["get", "title"])
    return f"# {title}\n\n{text[:8000]}" if "[error]" not in title else text[:8000]


@mcp.tool()
async def browser_search(query: str, max_results: int = 5) -> str:
    """One-shot: search the web via Google and return results with refs. Returns clickable results with titles and snippets."""
    url = f"https://www.google.com/search?q={query.replace(' ', '+')}&num={min(max_results, 10)}"
    out = _run(["open", url])
    if "[error]" in out:
        return out
    _run(["wait", "--load", "networkidle"])
    return _run(["snapshot", "-i"])


@mcp.tool()
async def browser_session_info() -> str:
    """Show current browser session information."""
    return _run(["session"])


if __name__ == "__main__":
    mcp.run()
