"""
prepare_nexus_datasets.py - Script para preparar datasets de entrenamiento

Convierte los datasets de SmolTalk y datos locales de SuperNEXUS
a formatos listos para SFT y DPO.

Uso:
    python prepare_nexus_datasets.py [--smoltalk-dir PATH] [--output-dir PATH]
    python prepare_nexus_datasets.py --collect-all
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Agregar proyecto al path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.data_collector import DataCollector
from src.security.atomic_io import atomic_write_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

NEXUS_HOME = Path(os.environ.get("NEXUS_HOME", Path.home() / ".nexus"))
BRAIN_DIR = NEXUS_HOME / "brain"
SMOLTALK_DIR = Path(os.environ.get("SMOLTALK_DIR", str(PROJECT_ROOT / "data" / "SmolTalk")))


def collect_all(output_dir: Path = None) -> dict:
    """Recolecta de todas las fuentes y exporta datasets"""
    output_dir = output_dir or NEXUS_HOME / "training_data"
    output_dir.mkdir(parents=True, exist_ok=True)

    collector = DataCollector(min_quality=0.7)

    # Cargar hashes existentes para evitar duplicados
    hash_file = output_dir / ".seen_hashes.txt"
    collector.deduplicator.load_existing_hashes(hash_file)

    logger.info("=== Recolectando datos de entrenamiento ===")

    # 1. Message board
    logger.info("Recolectando de message_board.db...")
    board_db = BRAIN_DIR / "message_board.db"
    if board_db.exists():
        collector.collect_from_message_board(board_db)
        logger.info(f"  -> {len(collector.samples)} muestras totales")

    # 2. Observations
    logger.info("Recolectando de nexus_memory.db...")
    memory_db = BRAIN_DIR / "nexus_memory.db"
    if memory_db.exists():
        collector.collect_from_observations(memory_db)
        logger.info(f"  -> {len(collector.samples)} muestras totales")

    # 3. SmolTalk datasets
    logger.info(f"Recolectando de SmolTalk ({SMOLTALK_DIR})...")
    if SMOLTALK_DIR.exists():
        collector.collect_from_smoltalk(SMOLTALK_DIR)
        logger.info(f"  -> {len(collector.samples)} muestras totales")

    # 4. Logs locales
    logger.info("Recolectando de logs...")
    log_dir = NEXUS_HOME / "logs"
    if log_dir.exists():
        collector.collect_from_logs(log_dir)
        logger.info(f"  -> {len(collector.samples)} muestras totales")

    # Estadisticas
    stats = collector.get_stats()
    logger.info("\n=== Estadisticas de recoleccion ===")
    logger.info(f"Total recolectado: {stats['total_collected']}")
    logger.info(f"Paso calidad: {stats['passed_quality']}")
    logger.info(f"Filtrado baja calidad: {stats['filtered_low_quality']}")
    logger.info(f"Filtrado duplicados: {stats['filtered_duplicate']}")
    logger.info(f"Calidad promedio: {stats['avg_quality']:.3f}")
    logger.info(f"Por categoria: {stats['by_category']}")

    # Exportar
    logger.info("\n=== Exportando datasets ===")

    # SFT completo
    sft_path = collector.export_sft(output_dir / "nexus_sft.jsonl", min_quality=0.7)
    logger.info(f"SFT dataset: {sft_path}")

    # DPO (solo alta calidad)
    dpo_path = collector.export_dpo(output_dir / "nexus_dpo.jsonl", min_quality=0.85)
    logger.info(f"DPO dataset: {dpo_path}")

    # Por categoria
    cat_dir = collector.export_by_category(output_dir / "by_category")
    logger.info(f"Datasets por categoria: {cat_dir}")

    # Guardar hashes
    collector.deduplicator.save_hashes(hash_file)

    # Guardar reporte
    report = {
        "collected_at": datetime.now().isoformat(),
        "stats": stats,
        "datasets": {
            "sft": str(sft_path),
            "dpo": str(dpo_path),
            "by_category": str(cat_dir),
        },
    }
    report_path = output_dir / "collection_report.json"
    atomic_write_json(report_path, report, indent=2)
    logger.info(f"Reporte guardado: {report_path}")

    return report


def validate_dataset(dataset_path: str) -> dict:
    """Valida un dataset existente"""
    path = Path(dataset_path)
    if not path.exists():
        return {"error": f"Archivo no existe: {dataset_path}"}

    samples = []
    errors = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            try:
                sample = json.loads(line)
                samples.append(sample)
            except json.JSONDecodeError as e:
                errors.append({"line": i, "error": str(e)})

    # Analizar contenido
    categories = {}
    quality_scores = []
    for s in samples:
        cat = s.get("category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1
        if "quality" in s:
            quality_scores.append(s["quality"])

    return {
        "valid": len(errors) == 0,
        "total_samples": len(samples),
        "errors": errors[:10],
        "categories": categories,
        "avg_quality": sum(quality_scores) / max(len(quality_scores), 1),
        "file_size_mb": round(path.stat().st_size / (1024 * 1024), 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Preparar datasets para NexusTrainer")
    parser.add_argument("--collect-all", action="store_true",
                       help="Recolectar de todas las fuentes")
    parser.add_argument("--smoltalk-dir", type=str, default=str(SMOLTALK_DIR),
                       help="Directorio de SmolTalk datasets")
    parser.add_argument("--output-dir", type=str,
                       default=str(NEXUS_HOME / "training_data"),
                       help="Directorio de salida")
    parser.add_argument("--validate", type=str,
                       help="Validar un dataset existente")
    parser.add_argument("--min-quality", type=float, default=0.7,
                       help="Calidad minima (0.0-1.0)")

    args = parser.parse_args()

    if args.collect_all:
        report = collect_all(Path(args.output_dir))
        print(f"\nDataset preparado: {report['datasets']['sft']}")
        print(f"Muestras SFT: {report['stats']['passed_quality']}")
    elif args.validate:
        result = validate_dataset(args.validate)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
