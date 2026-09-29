"""
Animated Themes - SuperNEXUS v2
Particle system canvas: rain, constellations, synapse, perlin-flow, sparkles, embers.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("NEXUS_DATA", Path.home() / ".nexus")) / "themes"
DATA_DIR.mkdir(parents=True, exist_ok=True)
THEMES_FILE = DATA_DIR / "themes.json"

BUILTIN_THEMES = {
    "dark": {
        "name": "Dark",
        "bg": "#0a0a0f",
        "fg": "#e0e0e0",
        "panel": "#12121a",
        "border": "#1e1e2e",
        "accent": "#00d4ff",
        "pattern": "none",
    },
    "midnight": {
        "name": "Midnight",
        "bg": "#0a0e1a",
        "fg": "#c8d0e0",
        "panel": "#10162a",
        "border": "#1a2240",
        "accent": "#6366f1",
        "pattern": "rain",
    },
    "cyberpunk": {
        "name": "Cyberpunk",
        "bg": "#0d0221",
        "fg": "#ff00ff",
        "panel": "#150530",
        "border": "#3d0066",
        "accent": "#00ffff",
        "pattern": "synapse",
    },
    "ocean": {
        "name": "Ocean",
        "bg": "#0a1628",
        "fg": "#b0c4de",
        "panel": "#0f1f3a",
        "border": "#1a3050",
        "accent": "#00bfff",
        "pattern": "constellations",
    },
    "forest": {
        "name": "Forest",
        "bg": "#0a1a0a",
        "fg": "#a0c8a0",
        "panel": "#0f200f",
        "border": "#1a3a1a",
        "accent": "#00ff88",
        "pattern": "sparkles",
    },
    "terminal": {
        "name": "Terminal",
        "bg": "#000000",
        "fg": "#00ff00",
        "panel": "#0a0a0a",
        "border": "#003300",
        "accent": "#00ff00",
        "pattern": "perlin-flow",
    },
    "cute": {
        "name": "Cute",
        "bg": "#1a0a1a",
        "fg": "#ffb6c1",
        "panel": "#2a152a",
        "border": "#4a2040",
        "accent": "#ff69b4",
        "pattern": "sparkles",
    },
    "retrowave": {
        "name": "Retrowave",
        "bg": "#1a0030",
        "fg": "#ff6ec7",
        "panel": "#2a0050",
        "border": "#4a0070",
        "accent": "#ff00ff",
        "pattern": "embers",
    },
}

PATTERNS = ["none", "dots", "synapse", "rain", "constellations", "perlin-flow", "sparkles", "embers"]

PATTERN_JS = """
// === SuperNEXUS Particle System ===
(function() {
  const canvas = document.getElementById('bg-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let particles = [];
  let connections = [];
  let w, h;
  const PATTERN = '__PATTERN__';
  const COLOR = '__COLOR__';
  const INTENSITY = __INTENSITY__;
  const SIZE = __SIZE__;

  function resize() {
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
  }
  resize();
  window.addEventListener('resize', resize);

  class Particle {
    constructor() {
      this.x = Math.random() * w;
      this.y = Math.random() * h;
      this.vx = (Math.random() - 0.5) * 2;
      this.vy = (Math.random() - 0.5) * 2;
      this.size = Math.random() * SIZE + 1;
      this.alpha = Math.random() * 0.5 + 0.3;
      this.life = Math.random() * 200 + 100;
      this.maxLife = this.life;
    }
    update() {
      this.x += this.vx;
      this.y += this.vy;
      this.life--;
      if (this.x < 0 || this.x > w) this.vx *= -1;
      if (this.y < 0 || this.y > h) this.vy *= -1;
    }
    draw() {
      const a = (this.life / this.maxLife) * this.alpha;
      ctx.beginPath();
      ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
      ctx.fillStyle = COLOR.replace(')', `,${a})`).replace('rgb', 'rgba');
      ctx.fill();
    }
  }

  function initParticles(count) {
    particles = [];
    for (let i = 0; i < count; i++) particles.push(new Particle());
  }

  function drawConnections() {
    for (let i = 0; i < particles.length; i++) {
      for (let j = i + 1; j < particles.length; j++) {
        const dx = particles[i].x - particles[j].x;
        const dy = particles[i].y - particles[j].y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 150) {
          ctx.beginPath();
          ctx.moveTo(particles[i].x, particles[i].y);
          ctx.lineTo(particles[j].x, particles[j].y);
          ctx.strokeStyle = COLOR.replace(')', `,${0.15 * (1 - dist / 150)})`).replace('rgb', 'rgba');
          ctx.lineWidth = 0.5;
          ctx.stroke();
        }
      }
    }
  }

  function animate() {
    ctx.clearRect(0, 0, w, h);
    particles.forEach(p => { p.update(); p.draw(); });
    if (PATTERN === 'synapse' || PATTERN === 'constellations') drawConnections();
    if (PATTERN === 'rain') particles.forEach(p => {
      if (p.y > h) { p.y = 0; p.x = Math.random() * w; }
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      ctx.lineTo(p.x, p.y + 15);
      ctx.strokeStyle = COLOR.replace(')', ',0.3)').replace('rgb', 'rgba');
      ctx.lineWidth = 1;
      ctx.stroke();
    });
    if (PATTERN === 'embers') particles.forEach(p => {
      p.vy -= 0.02;
      if (p.y < 0) { p.y = h; p.x = Math.random() * w; }
    });
    if (particles.length < INTENSITY * 50) particles.push(new Particle());
    requestAnimationFrame(animate);
  }

  initParticles(Math.floor(INTENSITY * 30));
  animate();
})();
"""


class ThemeManager:
    """Gestor de temas animados con particle system canvas."""

    def __init__(self):
        self.themes: Dict[str, Dict] = {}
        self.active_theme: str = "dark"
        self.pattern: str = "none"
        self.pattern_color: str = "rgb(0, 212, 255)"
        self.pattern_intensity: float = 0.5
        self.pattern_size: float = 2.0
        self.frosted: bool = False
        self._load()

    def _load(self):
        try:
            if THEMES_FILE.exists():
                data = json.loads(THEMES_FILE.read_text(encoding="utf-8"))
                self.themes = data.get("themes", {})
                self.active_theme = data.get("active", "dark")
                self.pattern = data.get("pattern", "none")
                self.pattern_color = data.get("pattern_color", "rgb(0, 212, 255)")
                self.pattern_intensity = data.get("pattern_intensity", 0.5)
                self.pattern_size = data.get("pattern_size", 2.0)
                self.frosted = data.get("frosted", False)
        except Exception as e:
            logger.error(f"Error cargando temas: {e}")

        for name, theme in BUILTIN_THEMES.items():
            if name not in self.themes:
                self.themes[name] = theme

    def _save(self):
        try:
            THEMES_FILE.write_text(json.dumps({
                "themes": self.themes,
                "active": self.active_theme,
                "pattern": self.pattern,
                "pattern_color": self.pattern_color,
                "pattern_intensity": self.pattern_intensity,
                "pattern_size": self.pattern_size,
                "frosted": self.frosted,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error guardando temas: {e}")

    def set_theme(self, name: str) -> bool:
        if name in self.themes:
            self.active_theme = name
            theme = self.themes[name]
            if "pattern" in theme:
                self.pattern = theme["pattern"]
            self._save()
            return True
        return False

    def set_pattern(self, pattern: str):
        if pattern in PATTERNS:
            self.pattern = pattern
            self._save()

    def create_theme(self, name: str, colors: Dict, pattern: str = "none") -> Dict:
        theme = {
            "name": name,
            "bg": colors.get("bg", "#000000"),
            "fg": colors.get("fg", "#ffffff"),
            "panel": colors.get("panel", "#111111"),
            "border": colors.get("border", "#222222"),
            "accent": colors.get("accent", "#00d4ff"),
            "pattern": pattern,
        }
        self.themes[name] = theme
        self._save()
        return theme

    def delete_theme(self, name: str) -> bool:
        if name in self.themes and name not in BUILTIN_THEMES:
            del self.themes[name]
            self._save()
            return True
        return False

    def list_themes(self) -> Dict[str, Dict]:
        return dict(self.themes)

    def get_active_css(self) -> Dict[str, str]:
        theme = self.themes.get(self.active_theme, BUILTIN_THEMES["dark"])
        return {
            "--color-bg": theme.get("bg", "#0a0a0f"),
            "--color-fg": theme.get("fg", "#e0e0e0"),
            "--color-panel": theme.get("panel", "#12121a"),
            "--color-border": theme.get("border", "#1e1e2e"),
            "--color-accent": theme.get("accent", "#00d4ff"),
        }

    def get_pattern_js(self) -> str:
        if self.pattern == "none":
            return ""
        return (PATTERN_JS
                .replace("__PATTERN__", self.pattern)
                .replace("__COLOR__", self.pattern_color)
                .replace("__INTENSITY__", str(self.pattern_intensity))
                .replace("__SIZE__", str(self.pattern_size)))

    def get_status(self) -> Dict:
        return {
            "active_theme": self.active_theme,
            "pattern": self.pattern,
            "frosted": self.frosted,
            "total_themes": len(self.themes),
        }
