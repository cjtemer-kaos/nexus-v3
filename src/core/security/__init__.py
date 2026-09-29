"""
Nexus Security Module - Unified access to security and resilience components.

This module provides unified access to:
- Credential Pool (LLM failover)
- Error Classification (smart retry)
- Tool Guardrails (safety controls)
- Trajectory Compression (token optimization)
- Jittered Backoff (resilient retries)
"""

from src.core.security.credential_pool import (
    CredentialPool,
    get_credential_pool,
    ProviderType,
    ModelInfo,
    FailoverStrategy,
)
from src.core.security.error_classifier import (
    ErrorClassifier,
    FailoverReason,
    ClassifiedError,
)
from src.core.security.tool_guardrails import (
    ToolGuardrails,
    get_tool_guardrails,
    IDEMPOTENT_TOOL_NAMES,
    MUTATING_TOOL_NAMES,
)
from src.core.security.trajectory_compressor import (
    TrajectoryCompressor,
    get_trajectory_compressor,
    Turn,
    TurnType,
)
from src.core.security.retry_utils import (
    jittered_backoff,
    get_retry_delay,
)


class NexusSecurity:
    """Unified security and resilience controller for SuperNEXUS."""
    
    def __init__(self):
        self.credential_pool = get_credential_pool()
        self.error_classifier = ErrorClassifier()
        self.tool_guardrails = get_tool_guardrails()
        self.compressor = get_trajectory_compressor()
    
    def check_command(self, command: str) -> tuple[bool, str]:
        return self.tool_guardrails.check_command_safety(command)
    
    def check_url(self, url: str) -> tuple[bool, str]:
        return self.tool_guardrails.check_url_safety(url)
    
    def classify_error(self, error: Exception, status_code: int = None, provider: str = "ollama", model: str = None) -> ClassifiedError:
        return ErrorClassifier.classify(error, status_code, provider, model)
    
    def get_retry_delay(self, attempt: int, error_type: str = "default") -> float:
        return get_retry_delay(attempt, error_type)
    
    def compress_if_needed(self, turns: list) -> list:
        return self.compressor.compress_if_needed(turns)
    
    def record_tool_call(self, tool_name: str, tool_input: any, result: str = "") -> None:
        self.tool_guardrails.record_tool_call(tool_name, tool_input, result)
    
    def check_loop(self) -> dict:
        return self.tool_guardrails.check_loop()
    
    def get_status(self) -> dict:
        return {
            "credential_pool": self.credential_pool.get_status(),
            "guardrails_active": True,
            "compressor_config": {
                "target_max_tokens": self.compressor.target_max_tokens,
                "preserve_first": self.compressor.preserve_first_n,
                "preserve_last": self.compressor.preserve_last_n,
            },
        }


_nexus_security: NexusSecurity = None


def get_nexus_security() -> NexusSecurity:
    global _nexus_security
    if _nexus_security is None:
        _nexus_security = NexusSecurity()
    return _nexus_security