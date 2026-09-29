"""
VisionGem v2 - Visión Artificial mejorada para SuperNEXUS v2.0

Características:
- Análisis de screenshots avanzado
- OCR para extracción de texto
- Detección de elementos de UI
- Descripción contextual de pantalla
- Integración con modelos de visión locales
"""

import base64
import logging
import time
from typing import Dict, List, Tuple
from dataclasses import dataclass
from pathlib import Path

try:
    from mss import mss
    from PIL import Image
    MSS_AVAILABLE = True
except:
    MSS_AVAILABLE = False

try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except:
    TESSERACT_AVAILABLE = False

try:
    import cv2
    OPENCV_AVAILABLE = True
except:
    OPENCV_AVAILABLE = False

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except:
    NUMPY_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass
class UIElement:
    """Elemento de UI detectado"""
    element_type: str
    x: int
    y: int
    width: int
    height: int
    confidence: float
    text: str = ""


@dataclass
class VisionResult:
    """Resultado de análisis de visión"""
    screenshot_path: str
    description: str
    ui_elements: List[UIElement]
    extracted_text: str
    dominant_colors: List[str]
    resolution: Tuple[int, int]
    analysis_time_ms: float


class VisionGem:
    """
    Visión Artificial mejorada para SuperNEXUS v2.0
    
    Uso:
        vision = VisionGem()
        result = await vision.analyze_screen("Describe la pantalla")
    """
    
    def __init__(self, screenshot_dir: str = None):
        import os
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else Path.home() / "screenshots"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        
        self.ollama_url = os.environ.get('OLLAMA_HOST', 'http://localhost:11434')
        self.vision_model = "qwen2.5vl:2b"
    
    def capture_screen(self, filename: str = None) -> Tuple[str, str, int, int]:
        """Captura pantalla"""
        if not MSS_AVAILABLE:
            raise RuntimeError("mss not available")
        
        with mss() as sct:
            monitor = sct.monitors[1]
            screenshot = sct.grab(monitor)
            img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
        
        if filename is None:
            filename = f"screen_{int(time.time())}.png"
        
        filepath = self.screenshot_dir / filename
        img.save(str(filepath))
        
        buffer = base64.b64encode(open(filepath, "rb").read()).decode()
        
        return str(filepath), buffer, screenshot.width, screenshot.height
    
    def extract_text(self, image_path: str) -> str:
        """Extrae texto de imagen usando OCR"""
        if not TESSERACT_AVAILABLE:
            logger.warning("Tesseract not available")
            return ""
        
        try:
            img = Image.open(image_path)
            text = pytesseract.image_to_string(img, lang="spa+eng")
            return text.strip()
        except Exception as e:
            logger.error(f"OCR error: {e}")
            return ""
    
    def detect_ui_elements(self, image_path: str) -> List[UIElement]:
        """Detecta elementos de UI"""
        if not OPENCV_AVAILABLE or not NUMPY_AVAILABLE:
            logger.warning("OpenCV or numpy not available")
            return []
        
        try:
            img = cv2.imread(image_path)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            edges = cv2.Canny(gray, 50, 150, apertureSize=3)
            
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            elements = []
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                
                if w > 50 and h > 20:
                    roi = gray[y:y+h, x:x+w]
                    text = pytesseract.image_to_string(Image.fromarray(roi), config="--psm 6").strip() if TESSERACT_AVAILABLE else ""
                    
                    element = UIElement(
                        element_type="button" if w < 200 and h < 50 else "panel",
                        x=x,
                        y=y,
                        width=w,
                        height=h,
                        confidence=0.7,
                        text=text,
                    )
                    elements.append(element)
            
            return elements[:20]
        except Exception as e:
            logger.error(f"UI detection error: {e}")
            return []
    
    def get_dominant_colors(self, image_path: str) -> List[str]:
        """Obtiene colores dominantes"""
        if not OPENCV_AVAILABLE or not NUMPY_AVAILABLE:
            return []
        
        try:
            img = cv2.imread(image_path)
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            img = cv2.resize(img, (100, 100))
            
            pixels = img.reshape(-1, 3)
            
            criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
            _, labels, centers = cv2.kmeans(
                np.float32(pixels),
                5,
                None,
                criteria,
                10,
                cv2.KMEANS_RANDOM_CENTERS,
            )
            
            colors = []
            for center in centers:
                r, g, b = int(center[0]), int(center[1]), int(center[2])
                colors.append(f"#{r:02x}{g:02x}{b:02x}")
            
            return colors
        except Exception as e:
            logger.error(f"Color extraction error: {e}")
            return []
    
    async def analyze_with_ollama(self, image_b64: str, prompt: str) -> str:
        """Analiza imagen con Ollama"""
        import httpx
        
        payload = {
            "model": self.vision_model,
            "prompt": prompt,
            "images": [image_b64],
            "stream": False,
        }
        
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self.ollama_url}/api/generate",
                    json=payload,
                )
                
                if response.status_code == 200:
                    return response.json().get("response", "")
                else:
                    return f"Error: HTTP {response.status_code}"
        except Exception as e:
            return f"Error: {e}"
    
    async def analyze_screen(self, prompt: str = "Describe lo que ves en la pantalla") -> VisionResult:
        """Analiza pantalla completa"""
        start_time = time.time()
        
        filepath, image_b64, width, height = self.capture_screen()
        
        extracted_text = self.extract_text(filepath)
        ui_elements = self.detect_ui_elements(filepath)
        dominant_colors = self.get_dominant_colors(filepath)
        
        description = await self.analyze_with_ollama(image_b64, prompt)
        
        analysis_time = (time.time() - start_time) * 1000
        
        result = VisionResult(
            screenshot_path=filepath,
            description=description,
            ui_elements=ui_elements,
            extracted_text=extracted_text,
            dominant_colors=dominant_colors,
            resolution=(width, height),
            analysis_time_ms=analysis_time,
        )
        
        logger.info(f"Screen analysis completed in {analysis_time:.0f}ms")
        
        return result
    
    def get_status(self) -> Dict:
        """Obtiene estado de visión"""
        return {
            "mss_available": MSS_AVAILABLE,
            "tesseract_available": TESSERACT_AVAILABLE,
            "opencv_available": OPENCV_AVAILABLE,
            "numpy_available": NUMPY_AVAILABLE,
            "vision_model": self.vision_model,
            "ollama_url": self.ollama_url,
            "screenshot_dir": str(self.screenshot_dir),
        }
