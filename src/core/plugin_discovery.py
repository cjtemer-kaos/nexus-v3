"""
Plugin Discovery - Dynamic plugin loading from multiple sources.
Absorbed from hermes-agent — names cleaned.
"""

import importlib
import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class PluginManifest:
    def __init__(self, data: Dict):
        self.name: str = data.get("name", "")
        self.kind: str = data.get("kind", "standalone")
        self.version: str = data.get("version", "0.1.0")
        self.description: str = data.get("description", "")
        self.provides_tools: List[str] = data.get("provides_tools", [])
        self.provides_hooks: List[str] = data.get("provides_hooks", [])
        self.requires_env: List[str] = data.get("requires_env", [])
        self.source: str = data.get("source", "unknown")
        self.path: Optional[str] = data.get("path")

    def to_dict(self) -> Dict:
        return {
            "name": self.name, "kind": self.kind, "version": self.version,
            "description": self.description, "provides_tools": self.provides_tools,
            "provides_hooks": self.provides_hooks, "source": self.source,
        }


class PluginContext:
    """Facade given to each plugin's register() function."""

    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._hooks: Dict[str, List[Callable]] = {}
        self._commands: Dict[str, Callable] = {}
        self._llm_caller: Optional[Callable] = None

    def register_tool(self, name: str, handler: Callable):
        self._tools[name] = handler

    def register_hook(self, name: str, callback: Callable):
        if name not in self._hooks:
            self._hooks[name] = []
        self._hooks[name].append(callback)

    def register_command(self, name: str, handler: Callable):
        self._commands[name] = handler

    @property
    def llm(self):
        return self._llm_caller

    def invoke_hook(self, name: str, **kwargs) -> List[Any]:
        results = []
        for cb in self._hooks.get(name, []):
            try:
                results.append(cb(**kwargs))
            except Exception as e:
                logger.error(f"Hook {name} error: {e}")
        return results

    def dispatch_tool(self, tool_name: str, **kwargs) -> Any:
        handler = self._tools.get(tool_name)
        if handler:
            return handler(**kwargs)
        return None


class PluginManager:
    """Multi-source hierarchical plugin discovery and loading."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or Path(os.environ.get("APP_DATA", Path.home() / ".app")) / "plugins"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._manifests: Dict[str, PluginManifest] = {}
        self._loaded: Dict[str, Any] = {}
        self._context = PluginContext()
        self._enabled: Set[str] = set()

    def discover(self, extra_dirs: Optional[List[Path]] = None) -> List[PluginManifest]:
        """Scan multiple sources for plugin manifests."""
        manifests = []

        if extra_dirs:
            for d in extra_dirs:
                manifests.extend(self._scan_directory(d, source="external"))

        manifests.extend(self._scan_directory(self.data_dir / "bundled", source="bundled"))
        manifests.extend(self._scan_directory(self.data_dir / "user", source="user"))

        project_dir = Path.cwd() / ".plugins"
        if project_dir.exists():
            manifests.extend(self._scan_directory(project_dir, source="project"))

        seen = {}
        for m in manifests:
            if m.name in seen:
                if m.source in ("user", "project"):
                    seen[m.name] = m
            else:
                seen[m.name] = m

        self._manifests = seen
        return list(seen.values())

    def _scan_directory(self, directory: Path, source: str) -> List[PluginManifest]:
        manifests = []
        if not directory.exists():
            return manifests
        for item in directory.iterdir():
            if item.is_dir():
                yaml_path = item / "plugin.yaml"
                if yaml_path.exists():
                    try:
                        import yaml
                        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                        data["source"] = source
                        data["path"] = str(item)
                        manifests.append(PluginManifest(data))
                    except Exception as e:
                        logger.warning(f"Failed to load manifest {yaml_path}: {e}")
                else:
                    for sub in item.iterdir():
                        if sub.is_dir():
                            sub_yaml = sub / "plugin.yaml"
                            if sub_yaml.exists():
                                try:
                                    import yaml
                                    data = yaml.safe_load(sub_yaml.read_text(encoding="utf-8"))
                                    data["source"] = source
                                    data["path"] = str(sub)
                                    manifests.append(PluginManifest(data))
                                except Exception:
                                    pass
        return manifests

    def load_plugin(self, name: str) -> bool:
        manifest = self._manifests.get(name)
        if not manifest or not manifest.path:
            return False
        if name in self._loaded:
            return True
        try:
            init_path = Path(manifest.path) / "__init__.py"
            if not init_path.exists():
                return False
            spec = importlib.util.spec_from_file_location(f"plugins.{name}", init_path)
            if not spec or not spec.loader:
                return False
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "register"):
                module.register(self._context)
            self._loaded[name] = module
            return True
        except Exception as e:
            logger.error(f"Failed to load plugin {name}: {e}")
            return False

    def enable(self, name: str):
        self._enabled.add(name)

    def disable(self, name: str):
        self._enabled.discard(name)

    def is_enabled(self, name: str) -> bool:
        return name in self._enabled

    @property
    def context(self) -> PluginContext:
        return self._context

    def list_plugins(self) -> List[Dict]:
        return [m.to_dict() for m in self._manifests.values()]

    def get_status(self) -> Dict:
        return {
            "total": len(self._manifests),
            "loaded": len(self._loaded),
            "enabled": len(self._enabled),
        }
