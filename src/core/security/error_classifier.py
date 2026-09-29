"""
Error Classification for smart failover and recovery.

Adaptado para SuperNEXUS v2.0

Provides structured taxonomy of API errors and recovery actions.
"""

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class FailoverReason(enum.Enum):
    """Why an API call failed — determines recovery strategy."""
    auth = "auth"
    auth_permanent = "auth_permanent"
    billing = "billing"
    rate_limit = "rate_limit"
    overloaded = "overloaded"
    server_error = "server_error"
    timeout = "timeout"
    context_overflow = "context_overflow"
    payload_too_large = "payload_too_large"
    image_too_large = "image_too_large"
    model_not_found = "model_not_found"
    model_quota = "model_quota"
    ollama_not_running = "ollama_not_running"
    ollama_model_not_found = "ollama_model_not_found"
    ollama_out_of_memory = "ollama_out_of_memory"
    format_error = "format_error"
    unknown = "unknown"


@dataclass
class ClassifiedError:
    """Structured classification of an API error with recovery hints."""
    reason: FailoverReason
    status_code: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    message: str = ""
    error_context: Dict[str, Any] = field(default_factory=dict)
    retryable: bool = True
    should_compress: bool = False
    should_rotate: bool = False
    should_fallback: bool = False
    retry_after: float = 0.0


class ErrorClassifier:
    """Classifier for API errors with recovery action hints."""

    RETRYABLE_CODES = {429, 500, 502, 503, 504}
    AUTH_CODES = {401, 403}
    RATE_LIMIT_CODES = {429, 402}

    @classmethod
    def classify(cls, error: Exception, status_code: Optional[int] = None, 
                 provider: str = "ollama", model: str = None) -> ClassifiedError:
        error_msg = str(error).lower()
        
        if status_code:
            if status_code in cls.AUTH_CODES:
                return cls._classify_auth_error(status_code, error_msg, provider)
            elif status_code in cls.RATE_LIMIT_CODES:
                return cls._classify_rate_limit(status_code, provider)
            elif status_code in cls.RETRYABLE_CODES:
                return cls._classify_server_error(status_code, provider)
            elif status_code == 404:
                return cls._classify_not_found(status_code, model, provider)
            elif status_code == 413:
                return cls._classify_payload_too_large(provider)
        
        return cls._classify_from_message(error, status_code, provider, model)

    @classmethod
    def _classify_auth_error(cls, status_code: int, error_msg: str, provider: str) -> ClassifiedError:
        if "invalid" in error_msg or "unauthorized" in error_msg:
            return ClassifiedError(
                reason=FailoverReason.auth_permanent,
                status_code=status_code,
                provider=provider,
                message="Authentication failed permanently",
                retryable=False,
            )
        return ClassifiedError(
            reason=FailoverReason.auth,
            status_code=status_code,
            provider=provider,
            message="Authentication error - may be transient",
            retryable=True,
            should_rotate=True,
            retry_after=30.0,
        )

    @classmethod
    def _classify_rate_limit(cls, status_code: int, provider: str) -> ClassifiedError:
        return ClassifiedError(
            reason=FailoverReason.rate_limit,
            status_code=status_code,
            provider=provider,
            message="Rate limited",
            retryable=True,
            should_rotate=True,
            retry_after=60.0,
        )

    @classmethod
    def _classify_server_error(cls, status_code: int, provider: str) -> ClassifiedError:
        if status_code == 503:
            return ClassifiedError(
                reason=FailoverReason.overloaded,
                status_code=status_code,
                provider=provider,
                message="Server overloaded",
                retryable=True,
                retry_after=30.0,
            )
        return ClassifiedError(
            reason=FailoverReason.server_error,
            status_code=status_code,
            provider=provider,
            message="Server error",
            retryable=True,
            retry_after=10.0,
        )

    @classmethod
    def _classify_not_found(cls, status_code: int, model: Optional[str], provider: str) -> ClassifiedError:
        if model:
            return ClassifiedError(
                reason=FailoverReason.model_not_found,
                status_code=status_code,
                provider=provider,
                model=model,
                message=f"Model {model} not found",
                retryable=False,
                should_fallback=True,
            )
        return ClassifiedError(
            reason=FailoverReason.unknown,
            status_code=status_code,
            provider=provider,
            message="Resource not found",
            retryable=False,
        )

    @classmethod
    def _classify_payload_too_large(cls, provider: str) -> ClassifiedError:
        return ClassifiedError(
            reason=FailoverReason.payload_too_large,
            status_code=413,
            provider=provider,
            message="Payload too large",
            retryable=True,
            should_compress=True,
        )

    @classmethod
    def _classify_from_message(cls, error: Exception, status_code: Optional[int],
                               provider: str, model: Optional[str]) -> ClassifiedError:
        error_msg = str(error).lower()
        
        if provider == "ollama":
            if "connection refused" in error_msg or "connect" in error_msg:
                return ClassifiedError(
                    reason=FailoverReason.ollama_not_running,
                    status_code=status_code,
                    provider=provider,
                    message="Ollama not running or not accessible",
                    retryable=True,
                    retry_after=5.0,
                )
            elif "model not found" in error_msg:
                return ClassifiedError(
                    reason=FailoverReason.ollama_model_not_found,
                    status_code=status_code,
                    provider=provider,
                    model=model,
                    message=f"Model {model or 'requested'} not found",
                    retryable=False,
                    should_fallback=True,
                )
            elif "out of memory" in error_msg or "oom" in error_msg:
                return ClassifiedError(
                    reason=FailoverReason.ollama_out_of_memory,
                    status_code=status_code,
                    provider=provider,
                    message="Ollama out of memory",
                    retryable=True,
                    should_fallback=True,
                    retry_after=30.0,
                )
        
        if "timeout" in error_msg or "timed out" in error_msg:
            return ClassifiedError(
                reason=FailoverReason.timeout,
                status_code=status_code,
                provider=provider,
                message="Request timed out",
                retryable=True,
                retry_after=10.0,
            )
        
        if "context" in error_msg and ("length" in error_msg or "size" in error_msg):
            return ClassifiedError(
                reason=FailoverReason.context_overflow,
                status_code=status_code,
                provider=provider,
                message="Context too large",
                retryable=True,
                should_compress=True,
            )
        
        return ClassifiedError(
            reason=FailoverReason.unknown,
            status_code=status_code,
            provider=provider,
            message=str(error),
            retryable=True,
            retry_after=5.0,
        )