"""
Gallery + Image Editor para SuperNEXUS v2.0

Inspirado en Odysseus:
- Galeria de imagenes con SQLite
- Upload con dedup SHA-256
- Transformaciones PIL: resize, rotate, crop, filters
- Face enhancement (GFPGAN fallback a PIL)
- Upscale local (Real-ESRGAN fallback)
- Albums y tags
- Metadata EXIF
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

# Directorio de imagenes
IMAGES_DIR = Path.home() / ".nexus" / "gallery"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class GalleryImage:
    """Imagen en la galeria."""
    id: str
    filename: str
    original_filename: str
    file_hash: str
    file_size: int
    width: int = 0
    height: int = 0
    prompt: str = ""
    tags: str = ""
    ai_tags: str = ""
    model: str = ""
    session_id: str = ""
    album_id: Optional[str] = None
    is_favorite: bool = False
    is_active: bool = True
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class GalleryAlbum:
    """Album de imagenes."""
    id: str
    name: str
    description: str = ""
    cover_image_id: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class Gallery:
    """Galeria de imagenes con editor integrado."""

    def __init__(self):
        self.images: Dict[str, GalleryImage] = {}
        self.albums: Dict[str, GalleryAlbum] = {}
        self._load_metadata()

    def _load_metadata(self):
        """Cargar metadata desde disco."""
        meta_file = IMAGES_DIR / "metadata.json"
        if meta_file.exists():
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                for img_data in data.get("images", []):
                    img = GalleryImage(**img_data)
                    self.images[img.id] = img
                for alb_data in data.get("albums", []):
                    alb = GalleryAlbum(**alb_data)
                    self.albums[alb.id] = alb
            except Exception as e:
                logger.warning(f"Error cargando metadata: {e}")

    def _save_metadata(self):
        """Guardar metadata a disco."""
        meta_file = IMAGES_DIR / "metadata.json"
        data = {
            "images": [asdict(img) for img in self.images.values()],
            "albums": [asdict(alb) for alb in self.albums.values()],
        }
        meta_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    async def upload(
        self,
        content: bytes,
        filename: str,
        prompt: str = "",
        tags: str = "",
        model: str = "",
        session_id: str = "",
        album_id: Optional[str] = None,
    ) -> Dict:
        """
        Subir imagen con dedup SHA-256.
        
        Returns:
            Dict con id, filename, duplicate info
        """
        file_hash = hashlib.sha256(content).hexdigest()

        # Dedup
        for img in self.images.values():
            if img.file_hash == file_hash and img.is_active:
                return {
                    "ok": False,
                    "duplicate": True,
                    "id": img.id,
                    "filename": img.filename,
                    "message": "Imagen duplicada omitida",
                }

        # Guardar archivo
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
        safe_filename = f"{uuid.uuid4().hex[:12]}.{ext}"
        img_path = IMAGES_DIR / safe_filename
        img_path.write_bytes(content)

        # Obtener dimensiones
        width, height = 0, 0
        try:
            from PIL import Image
            with Image.open(img_path) as img:
                width, height = img.size
        except Exception:
            pass

        # Crear registro
        img_id = str(uuid.uuid4())
        gallery_img = GalleryImage(
            id=img_id,
            filename=safe_filename,
            original_filename=filename,
            file_hash=file_hash,
            file_size=len(content),
            width=width,
            height=height,
            prompt=prompt,
            tags=tags,
            model=model,
            session_id=session_id,
            album_id=album_id,
        )
        self.images[img_id] = gallery_img
        self._save_metadata()

        return {
            "ok": True,
            "id": img_id,
            "filename": safe_filename,
            "width": width,
            "height": height,
        }

    def get_image(self, img_id: str) -> Optional[Dict]:
        """Obtener imagen por ID."""
        img = self.images.get(img_id)
        if not img or not img.is_active:
            return None
        return asdict(img)

    def get_library(
        self,
        search: Optional[str] = None,
        tag: Optional[str] = None,
        model: Optional[str] = None,
        album_id: Optional[str] = None,
        favorites_only: bool = False,
        limit: int = 24,
        offset: int = 0,
    ) -> Dict:
        """Obtener biblioteca con filtros."""
        items = [img for img in self.images.values() if img.is_active]

        if search:
            term = search.lower()
            items = [
                img for img in items
                if term in img.prompt.lower()
                or term in img.tags.lower()
                or term in img.ai_tags.lower()
            ]

        if tag:
            for t in tag.split(","):
                t = t.strip()
                if t:
                    items = [
                        img for img in items
                        if t in img.tags.lower() or t in img.ai_tags.lower()
                    ]

        if model:
            items = [img for img in items if img.model == model]

        if album_id:
            items = [img for img in items if img.album_id == album_id]

        if favorites_only:
            items = [img for img in items if img.is_favorite]

        items.sort(key=lambda x: x.created_at, reverse=True)
        total = len(items)
        items = items[offset:offset + limit]

        return {
            "images": [asdict(img) for img in items],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def set_favorite(self, img_id: str, favorite: bool) -> bool:
        """Marcar/desmarcar favorito."""
        img = self.images.get(img_id)
        if not img:
            return False
        img.is_favorite = favorite
        self._save_metadata()
        return True

    def delete_image(self, img_id: str) -> bool:
        """Eliminar imagen (soft delete)."""
        img = self.images.get(img_id)
        if not img:
            return False
        img.is_active = False
        self._save_metadata()
        return True

    # ==================== ALBUMS ====================

    def create_album(self, name: str, description: str = "") -> Dict:
        """Crear album."""
        alb_id = str(uuid.uuid4())
        album = GalleryAlbum(id=alb_id, name=name, description=description)
        self.albums[alb_id] = album
        self._save_metadata()
        return asdict(album)

    def get_albums(self) -> List[Dict]:
        """Listar albums."""
        return [asdict(alb) for alb in self.albums.values()]

    def add_to_album(self, img_id: str, album_id: str) -> bool:
        """Agregar imagen a album."""
        img = self.images.get(img_id)
        album = self.albums.get(album_id)
        if not img or not album:
            return False
        img.album_id = album_id
        self._save_metadata()
        return True

    # ==================== IMAGE EDITOR ====================

    def transform(
        self,
        img_id: str,
        operation: str,
        **params,
    ) -> Dict:
        """
        Transformar imagen.
        
        Operaciones:
            resize: width, height
            rotate: angle (grados)
            crop: left, top, right, bottom
            flip: direction ("horizontal" o "vertical")
            filter: filter_name (blur, sharpen, enhance, grayscale, sepia)
            brightness: factor (0.5-2.0)
            contrast: factor (0.5-2.0)
        """
        img_data = self.images.get(img_id)
        if not img_data or not img_data.is_active:
            return {"error": "Imagen no encontrada"}

        try:
            from PIL import Image, ImageFilter, ImageEnhance
            img_path = IMAGES_DIR / img_data.filename
            with Image.open(img_path) as img:
                if operation == "resize":
                    w = params.get("width", img.width)
                    h = params.get("height", img.height)
                    img = img.resize((w, h), Image.Resampling.LANCZOS)

                elif operation == "rotate":
                    angle = params.get("angle", 90)
                    img = img.rotate(angle, expand=True)

                elif operation == "crop":
                    left = params.get("left", 0)
                    top = params.get("top", 0)
                    right = params.get("right", img.width)
                    bottom = params.get("bottom", img.height)
                    img = img.crop((left, top, right, bottom))

                elif operation == "flip":
                    direction = params.get("direction", "horizontal")
                    if direction == "horizontal":
                        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                    else:
                        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

                elif operation == "filter":
                    filter_name = params.get("filter_name", "sharpen")
                    if filter_name == "blur":
                        img = img.filter(ImageFilter.GaussianBlur(radius=params.get("radius", 2)))
                    elif filter_name == "sharpen":
                        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
                    elif filter_name == "enhance":
                        img = img.filter(ImageFilter.MedianFilter(size=3))
                        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
                    elif filter_name == "grayscale":
                        img = img.convert("L").convert("RGB")
                    elif filter_name == "sepia":
                        grayscale = img.convert("L")
                        img = Image.merge("RGB", [
                            grayscale.point(lambda x: min(255, int(x * 1.2))),
                            grayscale.point(lambda x: min(255, int(x * 1.0))),
                            grayscale.point(lambda x: min(255, int(x * 0.8))),
                        ])

                elif operation == "brightness":
                    factor = params.get("factor", 1.0)
                    enhancer = ImageEnhance.Brightness(img)
                    img = enhancer.enhance(factor)

                elif operation == "contrast":
                    factor = params.get("factor", 1.0)
                    enhancer = ImageEnhance.Contrast(img)
                    img = enhancer.enhance(factor)

                else:
                    return {"error": f"Operacion no soportada: {operation}"}

                # Guardar resultado
                new_filename = f"{uuid.uuid4().hex[:12]}.png"
                new_path = IMAGES_DIR / new_filename
                img.save(new_path, "PNG")

                # Crear nuevo registro
                new_id = str(uuid.uuid4())
                new_img = GalleryImage(
                    id=new_id,
                    filename=new_filename,
                    original_filename=img_data.original_filename,
                    file_hash=hashlib.sha256(new_path.read_bytes()).hexdigest(),
                    file_size=new_path.stat().st_size,
                    width=img.width,
                    height=img.height,
                    prompt=img_data.prompt,
                    tags=img_data.tags,
                    model=img_data.model,
                    session_id=img_data.session_id,
                )
                self.images[new_id] = new_img
                self._save_metadata()

                return {
                    "ok": True,
                    "id": new_id,
                    "filename": new_filename,
                    "width": img.width,
                    "height": img.height,
                    "operation": operation,
                }

        except Exception as e:
            logger.error(f"Error en transformacion: {e}")
            return {"error": str(e)}

    def enhance_face(self, img_id: str) -> Dict:
        """
        Mejorar rostro/cara.
        Intenta GFPGAN, fallback a PIL enhancement.
        """
        img_data = self.images.get(img_id)
        if not img_data:
            return {"error": "Imagen no encontrada"}

        try:
            from PIL import Image, ImageFilter, ImageEnhance
            img_path = IMAGES_DIR / img_data.filename

            # Intentar GFPGAN
            try:
                from gfpgan import GFPGANer
                restorer = GFPGANer(
                    model_path="GFPGANv1.3.pth",
                    upscale=1,
                    arch="clean",
                    channel_multiplier=2,
                )
                with Image.open(img_path) as img:
                    import numpy as np
                    img_array = np.array(img)
                    _, _, output = restorer.enhance(img_array, has_aligned=False, only_center_face=False, paste_back=True)
                    result = Image.fromarray(output)

                    new_filename = f"{uuid.uuid4().hex[:12]}.png"
                    new_path = IMAGES_DIR / new_filename
                    result.save(new_path, "PNG")

                    new_id = str(uuid.uuid4())
                    new_img = GalleryImage(
                        id=new_id,
                        filename=new_filename,
                        original_filename=img_data.original_filename,
                        file_hash=hashlib.sha256(new_path.read_bytes()).hexdigest(),
                        file_size=new_path.stat().st_size,
                        width=result.width,
                        height=result.height,
                        prompt=img_data.prompt + " [face-enhanced]",
                        tags=img_data.tags,
                        model="gfpgan",
                    )
                    self.images[new_id] = new_img
                    self._save_metadata()

                    return {"ok": True, "id": new_id, "filename": new_filename, "method": "gfpgan"}

            except ImportError:
                logger.info("GFPGAN no disponible, usando PIL enhancement")

            # Fallback PIL
            with Image.open(img_path) as img:
                enhanced = img.filter(ImageFilter.MedianFilter(size=3))
                enhanced = enhanced.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
                enhanced = ImageEnhance.Contrast(enhanced).enhance(1.15)
                enhanced = ImageEnhance.Color(enhanced).enhance(1.1)
                enhanced = ImageEnhance.Brightness(enhanced).enhance(1.05)

                new_filename = f"{uuid.uuid4().hex[:12]}.png"
                new_path = IMAGES_DIR / new_filename
                enhanced.save(new_path, "PNG")

                new_id = str(uuid.uuid4())
                new_img = GalleryImage(
                    id=new_id,
                    filename=new_filename,
                    original_filename=img_data.original_filename,
                    file_hash=hashlib.sha256(new_path.read_bytes()).hexdigest(),
                    file_size=new_path.stat().st_size,
                    width=enhanced.width,
                    height=enhanced.height,
                    prompt=img_data.prompt + " [face-enhanced]",
                    tags=img_data.tags,
                    model="pil-enhance",
                )
                self.images[new_id] = new_img
                self._save_metadata()

                return {"ok": True, "id": new_id, "filename": new_filename, "method": "pil"}

        except Exception as e:
            return {"error": str(e)}

    def upscale_local(self, img_id: str, scale: int = 2) -> Dict:
        """
        Upscale local con Real-ESRGAN.
        Fallback a PIL resize si no esta disponible.
        """
        img_data = self.images.get(img_id)
        if not img_data:
            return {"error": "Imagen no encontrada"}

        try:
            from PIL import Image
            img_path = IMAGES_DIR / img_data.filename

            # Intentar Real-ESRGAN
            try:
                from realesrgan import RealESRGANer
                from basicsr.archs.rrdbnet_arch import RRDBNet

                model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4)
                upsampler = RealESRGANer(
                    scale=4,
                    model_path="RealESRGAN_x4plus.pth",
                    model=model,
                    tile=0,
                    tile_pad=10,
                    pre_pad=0,
                    half=True,
                )

                with Image.open(img_path) as img:
                    import numpy as np
                    img_array = np.array(img)
                    output, _ = upsampler.enhance(img_array, outscale=scale)
                    result = Image.fromarray(output)

                    new_filename = f"{uuid.uuid4().hex[:12]}.png"
                    new_path = IMAGES_DIR / new_filename
                    result.save(new_path, "PNG")

                    new_id = str(uuid.uuid4())
                    new_img = GalleryImage(
                        id=new_id,
                        filename=new_filename,
                        original_filename=img_data.original_filename,
                        file_hash=hashlib.sha256(new_path.read_bytes()).hexdigest(),
                        file_size=new_path.stat().st_size,
                        width=result.width,
                        height=result.height,
                        prompt=img_data.prompt + f" [upscaled {scale}x]",
                        tags=img_data.tags,
                        model="realesrgan",
                    )
                    self.images[new_id] = new_img
                    self._save_metadata()

                    return {"ok": True, "id": new_id, "filename": new_filename, "method": "realesrgan", "scale": scale}

            except ImportError:
                logger.info("Real-ESRGAN no disponible, usando PIL resize")

            # Fallback PIL
            with Image.open(img_path) as img:
                new_w = img.width * scale
                new_h = img.height * scale
                result = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                new_filename = f"{uuid.uuid4().hex[:12]}.png"
                new_path = IMAGES_DIR / new_filename
                result.save(new_path, "PNG")

                new_id = str(uuid.uuid4())
                new_img = GalleryImage(
                    id=new_id,
                    filename=new_filename,
                    original_filename=img_data.original_filename,
                    file_hash=hashlib.sha256(new_path.read_bytes()).hexdigest(),
                    file_size=new_path.stat().st_size,
                    width=result.width,
                    height=result.height,
                    prompt=img_data.prompt + f" [upscaled {scale}x]",
                    tags=img_data.tags,
                    model="pil-resize",
                )
                self.images[new_id] = new_img
                self._save_metadata()

                return {"ok": True, "id": new_id, "filename": new_filename, "method": "pil", "scale": scale}

        except Exception as e:
            return {"error": str(e)}

    def get_tags(self) -> List[str]:
        """Obtener todos los tags unicos."""
        tags = set()
        for img in self.images.values():
            if img.is_active:
                for t in (img.tags or "").split(","):
                    t = t.strip()
                    if t:
                        tags.add(t)
                for t in (img.ai_tags or "").split(","):
                    t = t.strip()
                    if t:
                        tags.add(t)
        return sorted(tags)

    def get_models(self) -> List[str]:
        """Obtener modelos unicos."""
        models = set()
        for img in self.images.values():
            if img.is_active and img.model:
                models.add(img.model)
        return sorted(models)
