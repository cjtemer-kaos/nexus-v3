"""
ErrorEnvelope - Clasificacion tipada de errores para SuperNEXUS

- Retryable vs non-retryable exception classification
- Error categorization con severidad y recoverability
- Permite retry inteligente en el Director

Cada error se envuelve en un ErrorEnvelope que decide:
1. Si se puede reintentar automaticamente
2. Cuantas veces reintentar
3. Si requiere intervencion humana
4. Si debe abortar la tarea
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    """Categoria del error"""
    NETWORK = "network"  # Timeout, connection refused, DNS failure
    LLM = "llm"  # Model error, context overflow, generation failure
    TOOL = "tool"  # Tool execution failed, invalid parameters
    FILESYSTEM = "filesystem"  # File not found, permission denied
    SYNTAX = "syntax"  # Code syntax error, parse error
    SECURITY = "security"  # Blocked command, SSRF attempt
    RESOURCE = "resource"  # OOM, disk full, rate limit
    LOGIC = "logic"  # Invalid state, assertion failure
    UNKNOWN = "unknown"


class ErrorSeverity(Enum):
    """Severidad del error"""
    LOW = "low"  # Cosmetic, non-blocking
    MEDIUM = "medium"  # Feature degraded, can continue
    HIGH = "high"  # Task blocked, needs intervention
    CRITICAL = "critical"  # System at risk, immediate action


class ErrorDecision(Enum):
    """Decision a tomar ante el error"""
    RETRY = "retry"  # Reintentar automaticamente
    RETRY_WITH_FIX = "retry_with_fix"  # Reintentar con ajuste
    SKIP = "skip"  # Saltar este paso, continuar
    ABORT = "abort"  # Abortar tarea completa
    ESCALATE = "escalate"  # Escalar a humano


@dataclass
class ErrorEnvelope:
    """Envoltorio tipado para errores con metadata de recuperacion"""
    error: Exception
    category: ErrorCategory = ErrorCategory.UNKNOWN
    severity: ErrorSeverity = ErrorSeverity.MEDIUM
    decision: ErrorDecision = ErrorDecision.RETRY
    is_retryable: bool = True
    max_retries: int = 3
    retry_count: int = 0
    retry_delay: float = 1.0  # segundos
    message: str = ""
    fix_suggestion: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    stack_trace: str = ""

    def __post_init__(self):
        if not self.message:
            self.message = str(self.error)

    def should_retry(self) -> bool:
        """Determina si se debe reintentar"""
        if not self.is_retryable:
            return False
        if self.decision == ErrorDecision.ABORT:
            return False
        if self.decision == ErrorDecision.ESCALATE:
            return False
        return self.retry_count < self.max_retries

    def get_retry_delay(self) -> float:
        """Calcula delay con backoff exponencial"""
        return self.retry_delay * (2 ** self.retry_count)

    def record_retry(self):
        """Registra un intento de retry"""
        self.retry_count += 1
        logger.info(f"Reintento {self.retry_count}/{self.max_retries} para {self.category.value}")

    def to_dict(self) -> Dict:
        """Serializa a dict para logging/storage"""
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "decision": self.decision.value,
            "is_retryable": self.is_retryable,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "message": self.message,
            "fix_suggestion": self.fix_suggestion,
            "context": self.context,
            "timestamp": self.timestamp,
        }


class ErrorClassifier:
    """Clasifica errores y genera ErrorEnvelopes"""

    # Patrones de errores retryables
    RETRYABLE_PATTERNS = [
        "timeout", "connection refused", "connection reset",
        "rate limit", "429", "503", "502", "504",
        "database is locked", "busy", "temporary",
        "model loading", "out of memory", "cuda out of memory",
        "context length", "context overflow",
    ]

    # Patrones de errores NO retryables
    NON_RETRYABLE_PATTERNS = [
        "permission denied", "access denied", "forbidden", "403",
        "not found", "404", "invalid api key", "authentication failed",
        "syntax error", "parse error", "invalid argument",
        "disk full", "no space left",
    ]

    # Patrones de seguridad
    SECURITY_PATTERNS = [
        "ssrf", "injection", "blocked", "blacklist",
        "unsafe", "forbidden command", "dangerous",
    ]

    @classmethod
    def classify(cls, error: Exception, context: Dict = None) -> ErrorEnvelope:
        """Clasifica un error y genera un ErrorEnvelope"""
        error_str = str(error).lower()
        error_type = type(error).__name__.lower()

        # Detectar categoria
        category = cls._detect_category(error_str, error_type)

        # Detectar severidad
        severity = cls._detect_severity(category, error_str)

        # Detectar si es retryable
        is_retryable = cls._is_retryable(error_str, error_type)

        # Determinar decision
        decision = cls._determine_decision(category, severity, is_retryable)

        # Generar sugerencia de fix
        fix_suggestion = cls._suggest_fix(category, error_str)

        # Configurar max retries segun categoria
        max_retries = {
            ErrorCategory.NETWORK: 5,
            ErrorCategory.LLM: 3,
            ErrorCategory.TOOL: 2,
            ErrorCategory.RESOURCE: 3,
            ErrorCategory.SECURITY: 0,  # Nunca reintentar errores de seguridad
        }.get(category, 1)

        return ErrorEnvelope(
            error=error,
            category=category,
            severity=severity,
            decision=decision,
            is_retryable=is_retryable,
            max_retries=max_retries,
            fix_suggestion=fix_suggestion,
            context=context or {},
        )

    @classmethod
    def _detect_category(cls, error_str: str, error_type: str) -> ErrorCategory:
        if any(p in error_str for p in cls.SECURITY_PATTERNS):
            return ErrorCategory.SECURITY
        if any(p in error_str for p in ["timeout", "connection", "dns", "network"]):
            return ErrorCategory.NETWORK
        if any(p in error_str for p in ["model", "llm", "generation", "context", "token"]):
            return ErrorCategory.LLM
        if any(p in error_str for p in ["file", "path", "directory", "permission"]):
            return ErrorCategory.FILESYSTEM
        if any(p in error_str for p in ["syntax", "parse", "invalid", "unexpected"]):
            return ErrorCategory.SYNTAX
        if any(p in error_str for p in ["memory", "disk", "resource", "oom"]):
            return ErrorCategory.RESOURCE
        if any(p in error_str for p in ["tool", "command", "execute"]):
            return ErrorCategory.TOOL
        return ErrorCategory.UNKNOWN

    @classmethod
    def _detect_severity(cls, category: ErrorCategory, error_str: str) -> ErrorSeverity:
        if category == ErrorCategory.SECURITY:
            return ErrorSeverity.CRITICAL
        if any(p in error_str for p in ["critical", "fatal", "crash"]):
            return ErrorSeverity.CRITICAL
        if category in (ErrorCategory.RESOURCE, ErrorCategory.LLM):
            return ErrorSeverity.HIGH
        if category in (ErrorCategory.NETWORK, ErrorCategory.TOOL):
            return ErrorSeverity.MEDIUM
        return ErrorSeverity.LOW

    @classmethod
    def _is_retryable(cls, error_str: str, error_type: str) -> bool:
        if any(p in error_str for p in cls.NON_RETRYABLE_PATTERNS):
            return False
        if any(p in error_str for p in cls.RETRYABLE_PATTERNS):
            return True
        # Por defecto, errores de IO y red son retryables
        if error_type in ("timeout", "connectionerror", "oserror", "ioerror"):
            return True
        return False

    @classmethod
    def _determine_decision(cls, category: ErrorCategory, severity: ErrorSeverity, is_retryable: bool) -> ErrorDecision:
        if category == ErrorCategory.SECURITY:
            return ErrorDecision.ABORT
        if severity == ErrorSeverity.CRITICAL and not is_retryable:
            return ErrorDecision.ESCALATE
        if is_retryable:
            return ErrorDecision.RETRY
        if severity == ErrorSeverity.LOW:
            return ErrorDecision.SKIP
        return ErrorDecision.ABORT

    @classmethod
    def _suggest_fix(cls, category: ErrorCategory, error_str: str) -> str:
        fixes = {
            ErrorCategory.NETWORK: "Verificar conexion de red y reintentar con backoff",
            ErrorCategory.LLM: "Reducir contexto o cambiar a modelo mas pequeno",
            ErrorCategory.TOOL: "Verificar parametros de la herramienta",
            ErrorCategory.FILESYSTEM: "Verificar permisos y ruta del archivo",
            ErrorCategory.SYNTAX: "Revisar sintaxis del codigo generado",
            ErrorCategory.SECURITY: "Accion bloqueada por politicas de seguridad",
            ErrorCategory.RESOURCE: "Liberar memoria o espacio en disco",
            ErrorCategory.LOGIC: "Revisar estado interno del agente",
        }
        return fixes.get(category, "No hay sugerencia automatica disponible")


class RetryManager:
    """Gestiona reintentos con backoff exponencial y ErrorEnvelope"""

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.history: List[ErrorEnvelope] = []

    def execute_with_retry(self, func, *args, **kwargs):
        """Ejecuta una funcion con reintentos inteligentes"""
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                envelope = ErrorClassifier.classify(e, context={"attempt": attempt})
                envelope.retry_count = attempt
                self.history.append(envelope)

                if not envelope.should_retry():
                    logger.error(f"Error no recuperable: {envelope.category.value} - {envelope.message}")
                    raise

                delay = envelope.get_retry_delay()
                logger.warning(f"Reintento {attempt + 1}/{envelope.max_retries} en {delay:.1f}s: {envelope.message}")
                time.sleep(delay)
                last_error = e

        raise last_error

    async def execute_with_retry_async(self, func, *args, **kwargs):
        """Version async del retry manager"""
        import asyncio
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                envelope = ErrorClassifier.classify(e, context={"attempt": attempt})
                envelope.retry_count = attempt
                self.history.append(envelope)

                if not envelope.should_retry():
                    logger.error(f"Error no recuperable: {envelope.category.value} - {envelope.message}")
                    raise

                delay = envelope.get_retry_delay()
                logger.warning(f"Reintento {attempt + 1}/{envelope.max_retries} en {delay:.1f}s: {envelope.message}")
                await asyncio.sleep(delay)
                last_error = e

        raise last_error

    def get_error_summary(self) -> Dict:
        """Resumen de errores encontrados"""
        by_category = {}
        by_decision = {}
        for env in self.history:
            cat = env.category.value
            dec = env.decision.value
            by_category[cat] = by_category.get(cat, 0) + 1
            by_decision[dec] = by_decision.get(dec, 0) + 1

        return {
            "total_errors": len(self.history),
            "by_category": by_category,
            "by_decision": by_decision,
            "recoverable": sum(1 for e in self.history if e.is_retryable),
            "unrecoverable": sum(1 for e in self.history if not e.is_retryable),
        }
