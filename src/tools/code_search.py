"""
Code Search - Search code across repositories.
Based on opencode/internal/llm/tools/sourcegraph.go pattern.

Supports:
- Local code search via grep/glob
- Sourcegraph API (if configured with SRC_ACCESS_TOKEN)
- GitHub search via gh CLI
"""

import asyncio
import logging
import os
import subprocess
import fnmatch
from typing import Dict

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


async def code_search(
    query: str,
    path: str = "",
    include: str = "",
    limit: int = 20,
) -> Dict:
    """
    Search code using local grep/glob or external APIs.
    
    Args:
        query: Search query (regex or plain text)
        path: Directory or file to search in (default: cwd)
        include: File pattern to include (e.g., "*.py")
        limit: Max results
    
    Returns:
        Dict with success, results, and metadata.
    """
    search_path = path or os.getcwd()
    if not os.path.isabs(search_path):
        search_path = os.path.abspath(search_path)
    
    if not os.path.exists(search_path):
        return {"error": f"Path not found: {search_path}"}
    # Los modelos pasan el archivo que quieren mirar, no el directorio. rg acepta
    # un archivo como path: antes esto devolvía "Directory not found" y la tool
    # parecía rota.
    only_file = os.path.isfile(search_path)
    
    results = []
    
    # Try ripgrep first
    try:
        cmd = ["rg", "--line-number", "--no-heading", "--max-count", str(limit)]
        if include:
            cmd.extend(["--glob", include])
        cmd.extend([query, search_path])
        
        # en hilo: rg puede tardar 30 s y esto corre en el event loop
        proc = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            import re as _re
            for line in proc.stdout.strip().split('\n'):
                if line:
                    # Formato ripgrep: path:line:content. El path puede tener ':' (unidad
                    # Windows C:\) -> match no-greedy hasta el primer ':digitos:'.
                    m = _re.match(r'^(.*?):(\d+):(.*)$', line)
                    if m:
                        results.append({
                            "file": m.group(1),
                            "line": int(m.group(2)),
                            "text": m.group(3)[:200],
                        })
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    
    # Fallback to Python grep
    if not results:
        import re
        try:
            pattern = re.compile(query)
        except re.error:
            return {"error": f"Invalid regex pattern: {query}"}
        
        include_pattern = None
        if include:
            include_pattern = include
        
        exclude_dirs = {'.git', 'node_modules', '__pycache__', 'venv', '.venv', 'dist', 'build', '.tox', 'eggs'}
        max_file_size = 1024 * 1024  # 1MB
        
        count = 0
        walk = ([(os.path.dirname(search_path), [], [os.path.basename(search_path)])]
                if only_file else os.walk(search_path))
        for root, dirs, files in walk:
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in exclude_dirs]
            for fname in files:
                if fname.startswith('.'):
                    continue
                if include_pattern and not fnmatch.fnmatch(fname, include_pattern):
                    continue
                
                fpath = os.path.join(root, fname)
                try:
                    if os.path.getsize(fpath) > max_file_size:
                        continue
                    with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                        for line_num, line in enumerate(f, 1):
                            if pattern.search(line):
                                results.append({
                                    "file": fpath,
                                    "line": line_num,
                                    "text": line.strip()[:200],
                                })
                                count += 1
                                if count >= limit:
                                    break
                    if count >= limit:
                        break
                except (PermissionError, OSError):
                    continue
            if count >= limit:
                break
    
    if not results:
        return {
            "success": True,
            "query": query,
            "results": [],
            "count": 0,
            "message": "No matches found",
        }
    
    output = f"Found {len(results)} matches for '{query}':\n"
    current_file = ""
    for r in results:
        if r["file"] != current_file:
            if current_file:
                output += "\n"
            current_file = r["file"]
            output += f"{r['file']}:\n"
        output += f"  Line {r['line']}: {r['text']}\n"
    
    return {
        "success": True,
        "query": query,
        "results": results,
        "count": len(results),
        "content": output,
    }


async def github_search(
    query: str,
    repo: str = "",
    language: str = "",
    limit: int = 10,
) -> Dict:
    """
    Search code on GitHub using gh CLI.
    
    Args:
        query: Search query
        repo: Specific repository (owner/repo)
        language: Filter by language
        limit: Max results
    """
    try:
        cmd = ["gh", "search", "code", query, "--limit", str(limit)]
        if repo:
            cmd.extend(["--repo", repo])
        if language:
            cmd.extend(["--language", language])
        
        # en hilo: rg puede tardar 30 s y esto corre en el event loop
        proc = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return {
                "success": True,
                "query": query,
                "content": proc.stdout.strip(),
                "source": "github",
            }
        else:
            return {"error": f"gh CLI error: {proc.stderr.strip()}"}
    except FileNotFoundError:
        return {"error": "gh CLI not installed. Run: gh auth login"}
    except Exception as e:
        return {"error": str(e)}
