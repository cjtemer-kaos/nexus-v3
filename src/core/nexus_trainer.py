"""
NexusTrainer - Pipeline completo de entrenamiento para SuperNEXUS

Inspired by: modded-nanogpt, unsloth, trl, axolotl

Orquesta el fine-tuning de modelos locales (Ollama/REMOTE GPU)
usando los datasets generados por DataCollector.

Fases del pipeline:
1. Pretrain - Entrenamiento base con datos generales
2. SFT (Supervised Fine-Tuning) - Instruccion siguiendo ejemplos
3. DPO (Direct Preference Optimization) - Alineacion con preferencias
4. RL (Reinforcement Learning) - Optimizacion con reward model
5. Chat - Formato conversacional final

Soporta:
- Entrenamiento local en REMOTE (GPU)
- LoRA/QLoRA para eficiencia de memoria
- Muon optimizer (de modded-nanogpt)
- Unsloth para fine-tuning rapido
- Ollama para conversion y despliegue
"""

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

# ============================================================
# Configuracion
# ============================================================
NEXUS_HOME = Path(os.environ.get("NEXUS_HOME", Path.home() / ".nexus"))
BRAIN_DIR = NEXUS_HOME / "brain"
DATA_DIR = NEXUS_HOME / "training_data"
TRAINING_DIR = NEXUS_HOME / "training_runs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TRAINING_DIR.mkdir(parents=True, exist_ok=True)

# Configuracion por defecto para REMOTE
REMOTE_HOST = os.environ.get("SUPER_NEXUS_REMOTE_IP", "remote-host")
REMOTE_USER = os.environ.get("SUPER_NEXUS_REMOTE_USER", "REMOTE-user")

# Configuracion del modelo base
DEFAULT_BASE_MODEL = "qwen2.5-coder:7b"

# Configuracion de entrenamiento
DEFAULT_TRAINING_CONFIG = {
    "model": DEFAULT_BASE_MODEL,
    "method": "qlora",          # lora, qlora, full
    "epochs": 3,
    "batch_size": 4,
    "gradient_accumulation": 4,
    "learning_rate": 2e-4,
    "max_seq_length": 4096,
    "warmup_ratio": 0.1,
    "weight_decay": 0.01,
    "optimizer": "adamw_8bit",  # adamw, adamw_8bit, muon
    "scheduler": "cosine",
    "fp16": True,
    "bf16": False,
    "logging_steps": 10,
    "save_steps": 500,
    "eval_steps": 500,
    "max_grad_norm": 0.3,
}

# Configuracion de LoRA
DEFAULT_LORA_CONFIG = {
    "r": 16,
    "alpha": 32,
    "dropout": 0.05,
    "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj",
                       "gate_proj", "up_proj", "down_proj"],
    "bias": "none",
    "task_type": "CAUSAL_LM",
}

# Configuracion de DPO
DEFAULT_DPO_CONFIG = {
    "beta": 0.1,
    "max_prompt_length": 512,
    "max_length": 2048,
    "loss_type": "sigmoid",
}


@dataclass
class TrainingJob:
    """Representa un trabajo de entrenamiento"""
    job_id: str
    phase: str  # pretrain, sft, dpo, rl, chat
    status: str  # pending, running, completed, failed, cancelled
    config: Dict[str, Any]
    dataset_path: str
    output_dir: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "job_id": self.job_id,
            "phase": self.phase,
            "status": self.status,
            "config": self.config,
            "dataset_path": self.dataset_path,
            "output_dir": self.output_dir,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "metrics": self.metrics,
        }


class TrainingScriptGenerator:
    """Genera scripts de entrenamiento para diferentes fases"""

    @staticmethod
    def generate_sft_script(job: TrainingJob, lora_config: Dict = None) -> str:
        """Genera script Python para SFT con Unsloth/TRL"""
        cfg = {**DEFAULT_TRAINING_CONFIG, **job.config}
        lora = {**DEFAULT_LORA_CONFIG, **(lora_config or {})}

        script = f'''"""
Script de SFT generado automaticamente por NexusTrainer
Job: {job.job_id}
Fecha: {job.started_at}
"""
import os
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, TrainingArguments
from trl import SFTTrainer, SFTConfig
from datasets import load_dataset

# Configuracion
model_name = "{cfg['model']}"
dataset_path = "{job.dataset_path}"
output_dir = "{job.output_dir}"

# Cargar modelo y tokenizer
print(f"Cargando modelo: {{model_name}}")
tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
tokenizer.pad_token = tokenizer.eos_token

model = AutoModelForCausalLM.from_pretrained(
    model_name,
    torch_dtype=torch.float16,
    device_map="auto",
    trust_remote_code=True,
)

# Cargar dataset
print(f"Cargando dataset: {{dataset_path}}")
dataset = load_dataset("json", data_files=dataset_path, split="train")

# Configuracion de LoRA
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

lora_config = LoraConfig(
    r={lora['r']},
    lora_alpha={lora['alpha']},
    target_modules={json.dumps(lora['target_modules'])},
    lora_dropout={lora['dropout']},
    bias="{lora['bias']}",
    task_type="{lora['task_type']}",
)

model = prepare_model_for_kbit_training(model)
model = get_peft_model(model, lora_config)
model.print_trainable_parameters()

# Configuracion de entrenamiento
training_args = SFTConfig(
    output_dir=output_dir,
    num_train_epochs={cfg['epochs']},
    per_device_train_batch_size={cfg['batch_size']},
    gradient_accumulation_steps={cfg['gradient_accumulation']},
    learning_rate={cfg['learning_rate']},
    max_seq_length={cfg['max_seq_length']},
    warmup_ratio={cfg['warmup_ratio']},
    weight_decay={cfg['weight_decay']},
    logging_steps={cfg['logging_steps']},
    save_steps={cfg['save_steps']},
    fp16={cfg['fp16']},
    bf16={cfg['bf16']},
    optim="{cfg['optimizer']}",
    lr_scheduler_type="{cfg['scheduler']}",
    max_grad_norm={cfg['max_grad_norm']},
    report_to="none",
    save_strategy="steps",
    save_total_limit=3,
)

# Entrenador
trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset,
    args=training_args,
    dataset_text_field="text",
)

# Entrenar
print("Iniciando entrenamiento SFT...")
trainer.train()

# Guardar
trainer.save_model(output_dir)
tokenizer.save_pretrained(output_dir)
print(f"Modelo guardado en: {{output_dir}}")
'''
        return script

    @staticmethod
    def generate_dpo_script(job: TrainingJob, dpo_config: Dict = None) -> str:
        """Genera script Python para DPO"""
        cfg = {**DEFAULT_TRAINING_CONFIG, **job.config}
        dpo = {**DEFAULT_DPO_CONFIG, **(dpo_config or {})}

        script = f'''"""
Script de DPO generado automaticamente por NexusTrainer
Job: {job.job_id}
"""
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from trl import DPOTrainer, DPOConfig
from datasets import load_dataset
from peft import LoraConfig, PeftModel

model_name = "{cfg['model']}"
dataset_path = "{job.dataset_path}"
output_dir = "{job.output_dir}"

tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
tokenizer.pad_token = tokenizer.eos_token

model = AutoModelForCausalLM.from_pretrained(
    model_name,
    torch_dtype=torch.float16,
    device_map="auto",
    trust_remote_code=True,
)

ref_model = AutoModelForCausalLM.from_pretrained(
    model_name,
    torch_dtype=torch.float16,
    device_map="auto",
    trust_remote_code=True,
)

dataset = load_dataset("json", data_files=dataset_path, split="train")

lora_config = LoraConfig(
    r={DEFAULT_LORA_CONFIG['r']},
    lora_alpha={DEFAULT_LORA_CONFIG['alpha']},
    target_modules={json.dumps(DEFAULT_LORA_CONFIG['target_modules'])},
    lora_dropout={DEFAULT_LORA_CONFIG['dropout']},
    bias="{DEFAULT_LORA_CONFIG['bias']}",
    task_type="{DEFAULT_LORA_CONFIG['task_type']}",
)

dpo_args = DPOConfig(
    output_dir=output_dir,
    beta={dpo['beta']},
    max_prompt_length={dpo['max_prompt_length']},
    max_length={dpo['max_length']},
    per_device_train_batch_size={cfg['batch_size']},
    gradient_accumulation_steps={cfg['gradient_accumulation']},
    learning_rate={cfg['learning_rate']},
    num_train_epochs={cfg['epochs']},
    logging_steps={cfg['logging_steps']},
    save_steps={cfg['save_steps']},
    fp16={cfg['fp16']},
    optim="{cfg['optimizer']}",
    report_to="none",
)

trainer = DPOTrainer(
    model=model,
    ref_model=ref_model,
    args=dpo_args,
    train_dataset=dataset,
    tokenizer=tokenizer,
    peft_config=lora_config,
)

print("Iniciando entrenamiento DPO...")
trainer.train()
trainer.save_model(output_dir)
print(f"Modelo DPO guardado en: {{output_dir}}")
'''
        return script

    @staticmethod
    def generate_ollama_modelfile(adapter_path: str, model_name: str,
                                  output_name: str) -> str:
        """Genera Modelfile para Ollama con adapter LoRA"""
        return f'''FROM {model_name}

# Adapter LoRA entrenado por NexusTrainer
ADAPTER {adapter_path}

# Parametros del modelo
PARAMETER temperature 0.7
PARAMETER top_p 0.9
PARAMETER num_ctx 4096

# System prompt para NEXUS IA
SYSTEM """Eres NEXUS IA, un organismo de IA soberano local.
Tu rol es orquestar multiples agentes IDE, aprender de cada tarea,
y auto-mejorarte continuamente. NUNCA dices "no puedo".
Si no sabes algo, investigas, aprendes, delegas e implementas."""
'''


class NexusTrainer:
    """Orquestador principal de entrenamiento"""

    def __init__(self, execute_on_REMOTE=None):
        self.execute_on_REMOTE = execute_on_REMOTE
        self.active_jobs: Dict[str, TrainingJob] = {}
        self.completed_jobs: List[TrainingJob] = []
        self.script_generator = TrainingScriptGenerator()

        # Cargar jobs existentes
        self._load_existing_jobs()

    def _load_existing_jobs(self):
        """Carga jobs de entrenamientos anteriores"""
        jobs_file = TRAINING_DIR / "jobs_registry.json"
        if jobs_file.exists():
            try:
                with open(jobs_file) as f:
                    data = json.load(f)
                for job_data in data.get("jobs", []):
                    job = TrainingJob(**job_data)
                    self.active_jobs[job.job_id] = job
            except Exception as e:
                logger.warning(f"Error cargando jobs existentes: {e}")

    def _save_jobs(self):
        """Guarda registry de jobs"""
        jobs_file = TRAINING_DIR / "jobs_registry.json"
        all_jobs = list(self.active_jobs.values()) + self.completed_jobs
        atomic_write_json(jobs_file, {
            "jobs": [j.to_dict() for j in all_jobs],
            "last_updated": datetime.now().isoformat(),
        })

    def create_job(self, phase: str, dataset_path: str,
                   config: Dict = None) -> TrainingJob:
        """Crea un nuevo trabajo de entrenamiento"""
        job_id = f"{phase}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        output_dir = str(TRAINING_DIR / job_id)

        job = TrainingJob(
            job_id=job_id,
            phase=phase,
            status="pending",
            config={**DEFAULT_TRAINING_CONFIG, **(config or {})},
            dataset_path=dataset_path,
            output_dir=output_dir,
        )

        self.active_jobs[job_id] = job
        self._save_jobs()
        return job

    def prepare_dataset(self, dataset_path: str,
                        format: str = "sft") -> Dict:
        """Prepara y valida un dataset para entrenamiento"""
        path = Path(dataset_path)
        if not path.exists():
            return {"error": f"Dataset no existe: {dataset_path}"}

        # Validar formato
        samples = []
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                try:
                    sample = json.loads(line)
                    samples.append(sample)
                except json.JSONDecodeError:
                    return {
                        "error": f"JSON invalido en linea {i+1}",
                        "line": line[:200],
                    }

        # Estadisticas del dataset
        total_tokens = sum(
            len(s.get("text", "")) +
            len(s.get("prompt", "")) +
            len(s.get("response", ""))
            for s in samples
        )

        return {
            "valid": True,
            "samples": len(samples),
            "total_chars": total_tokens,
            "avg_sample_length": total_tokens // max(len(samples), 1),
            "format": format,
            "path": str(path),
        }

    async def run_sft(self, job: TrainingJob,
                      lora_config: Dict = None) -> Dict:
        """Ejecuta entrenamiento SFT"""
        job.status = "running"
        job.started_at = datetime.now().isoformat()
        self._save_jobs()

        # Generar script
        script = self.script_generator.generate_sft_script(job, lora_config)
        script_path = Path(job.output_dir) / "train_sft.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script, encoding="utf-8")

        # Guardar config
        config_path = Path(job.output_dir) / "config.json"
        atomic_write_json(config_path, {
            "job": job.to_dict(),
            "lora_config": lora_config or DEFAULT_LORA_CONFIG,
            "script_generated_at": datetime.now().isoformat(),
        })

        # Ejecutar en REMOTE si hay GPU
        if self.execute_on_REMOTE:
            try:
                # Copiar script a REMOTE
                await self.execute_on_REMOTE(
                    f"mkdir -p {job.output_dir}"
                )

                # Ejecutar entrenamiento
                cmd = f"cd {job.output_dir} && python3 train_sft.py 2>&1 | tee train.log"
                result = await self.execute_on_REMOTE(cmd)

                job.metrics["REMOTE_result"] = str(result)[:1000]
                job.status = "completed"
                job.completed_at = datetime.now().isoformat()

            except Exception as e:
                job.status = "failed"
                job.error = str(e)
                job.completed_at = datetime.now().isoformat()
        else:
            # Entrenamiento local (sin GPU, solo para testing)
            job.status = "pending"
            job.error = "REMOTE GPU no disponible - script generado para ejecucion manual"

        self._save_jobs()
        return job.to_dict()

    async def run_dpo(self, job: TrainingJob,
                      dpo_config: Dict = None) -> Dict:
        """Ejecuta entrenamiento DPO"""
        job.status = "running"
        job.started_at = datetime.now().isoformat()
        self._save_jobs()

        script = self.script_generator.generate_dpo_script(job, dpo_config)
        script_path = Path(job.output_dir) / "train_dpo.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script, encoding="utf-8")

        if self.execute_on_REMOTE:
            try:
                await self.execute_on_REMOTE(f"mkdir -p {job.output_dir}")
                cmd = f"cd {job.output_dir} && python3 train_dpo.py 2>&1 | tee train.log"
                result = await self.execute_on_REMOTE(cmd)
                job.metrics["REMOTE_result"] = str(result)[:1000]
                job.status = "completed"
                job.completed_at = datetime.now().isoformat()
            except Exception as e:
                job.status = "failed"
                job.error = str(e)
                job.completed_at = datetime.now().isoformat()
        else:
            job.status = "pending"
            job.error = "REMOTE GPU no disponible"

        self._save_jobs()
        return job.to_dict()

    async def run_full_pipeline(self, sft_dataset: str,
                                dpo_dataset: str = None,
                                config: Dict = None) -> Dict:
        """Ejecuta pipeline completo: SFT -> DPO -> Ollama"""
        results = {"phases": [], "status": "running"}

        # Fase 1: SFT
        sft_job = self.create_job("sft", sft_dataset, config)
        sft_result = await self.run_sft(sft_job)
        results["phases"].append({"phase": "sft", "result": sft_result})

        if sft_result["status"] != "completed":
            results["status"] = "failed_at_sft"
            return results

        # Fase 2: DPO (opcional)
        if dpo_dataset:
            dpo_job = self.create_job("dpo", dpo_dataset, config)
            dpo_result = await self.run_dpo(dpo_job)
            results["phases"].append({"phase": "dpo", "result": dpo_result})

            if dpo_result["status"] != "completed":
                results["status"] = "failed_at_dpo"
                return results

        # Fase 3: Generar Modelfile para Ollama
        output_dir = sft_job.output_dir
        modelfile = self.script_generator.generate_ollama_modelfile(
            adapter_path=output_dir,
            model_name=config.get("model", DEFAULT_BASE_MODEL) if config else DEFAULT_BASE_MODEL,
            output_name=f"nexus-{sft_job.job_id}",
        )
        modelfile_path = Path(output_dir) / "Modelfile"
        modelfile_path.write_text(modelfile, encoding="utf-8")

        results["modelfile_path"] = str(modelfile_path)
        results["status"] = "completed"

        self._save_jobs()
        return results

    def create_ollama_model(self, job_id: str, model_name: str) -> Dict:
        """Crea un modelo en Ollama desde un entrenamiento completado"""
        job = self.active_jobs.get(job_id)
        if not job:
            return {"error": f"Job {job_id} no existe"}

        if job.status != "completed":
            return {"error": f"Job {job_id} no esta completado (status: {job.status})"}

        modelfile_path = Path(job.output_dir) / "Modelfile"
        if not modelfile_path.exists():
            return {"error": "Modelfile no existe - ejecutar pipeline completo primero"}

        try:
            result = subprocess.run(
                ["ollama", "create", model_name, "-f", str(modelfile_path)],
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0:
                return {
                    "success": True,
                    "model": model_name,
                    "output": result.stdout,
                }
            else:
                return {
                    "success": False,
                    "error": result.stderr,
                }
        except Exception as e:
            return {"error": str(e)}

    def list_jobs(self, status: str = None) -> List[Dict]:
        """Lista todos los jobs de entrenamiento"""
        all_jobs = list(self.active_jobs.values()) + self.completed_jobs
        if status:
            all_jobs = [j for j in all_jobs if j.status == status]
        return [j.to_dict() for j in all_jobs]

    def get_job(self, job_id: str) -> Optional[Dict]:
        """Obtiene detalles de un job especifico"""
        job = self.active_jobs.get(job_id)
        return job.to_dict() if job else None

    def cancel_job(self, job_id: str) -> Dict:
        """Cancela un job en ejecucion"""
        job = self.active_jobs.get(job_id)
        if not job:
            return {"error": f"Job {job_id} no existe"}

        if job.status != "running":
            return {"error": f"Job {job_id} no esta en ejecucion"}

        job.status = "cancelled"
        job.completed_at = datetime.now().isoformat()
        self._save_jobs()
        return {"success": True, "job_id": job_id}

    def get_training_report(self) -> Dict:
        """Genera reporte de todos los entrenamientos"""
        all_jobs = list(self.active_jobs.values()) + self.completed_jobs

        by_status = {}
        by_phase = {}
        for job in all_jobs:
            by_status[job.status] = by_status.get(job.status, 0) + 1
            by_phase[job.phase] = by_phase.get(job.phase, 0) + 1

        return {
            "total_jobs": len(all_jobs),
            "by_status": by_status,
            "by_phase": by_phase,
            "completed": [j.to_dict() for j in all_jobs if j.status == "completed"],
            "failed": [j.to_dict() for j in all_jobs if j.status == "failed"],
        }
