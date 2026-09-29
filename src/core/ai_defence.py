"""
AIDefence - Sistema de detección de ataques a IA (MEDUSA: 500+ patrones)

Inspirado en:
- MEDUSA: 9,600+ patrones de seguridad
- OWASP Top 10 for LLM
- Prompt injection research (Garak, Lakera, etc.)
- MCP security best practices
- Supply chain security for AI

Detecciones (15 categorías, 500+ patrones):
1. Prompt Injection (directa e indirecta) - 60 patrones
2. Jailbreak patterns (DAN, role manipulation, etc.) - 50 patrones
3. Data Exfiltration (URL encoding, base64, etc.) - 40 patrones
4. Sandbox Escape (path traversal, command injection) - 50 patrones
5. MCP Tool Poisoning (malicious tool definitions) - 30 patrones
6. Repo Poisoning (weaponized AI editor configs) - 40 patrones
7. Unicode Obfuscation (invisible characters) - 20 patrones
8. Adversarial Variable Naming - 30 patrones
9. Social Engineering / Manipulation - 40 patrones
10. Token Smuggling / Encoding Attacks - 30 patrones
11. Context Window Overflow Attacks - 20 patrones
12. Model Extraction / Distillation - 25 patrones
13. Training Data Poisoning - 25 patrones
14. API Abuse / Rate Limit Evasion - 20 patrones
15. Supply Chain / Dependency Attacks - 30 patrones

Arquitectura:
- Pattern matching regex (rápido, determinista)
- Semantic analysis (LLM-based, para casos complejos)
- Multi-agent consensus (para decisiones críticas)
"""

import re
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


class ThreatLevel(Enum):
    """Niveles de amenaza"""
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ThreatDetection:
    """Resultado de detección de amenaza"""
    threat_level: ThreatLevel
    threat_type: str
    confidence: float
    description: str
    matched_patterns: List[str] = field(default_factory=list)
    mitigations: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class AIDefence:
    """
    Sistema de detección de ataques a IA.
    
    Uso:
        defence = AIDefence()
        result = defence.scan(user_input)
        if result.threat_level != ThreatLevel.SAFE:
            # Apply mitigations
    """
    
    # ============================================================
    # PATRONES DE DETECCIÓN (500+ patrones en 15 categorías)
    # ============================================================
    
    # 1. Prompt Injection Patterns (60 patrones)
    PROMPT_INJECTION = [
        r"(?i)ignore\s+(previous|all|above|these|my|the|following)\s+(instructions|rules|prompts|directives|guidelines|constraints|settings|configuration)",
        r"(?i)forget\s+(your\s+)?(instructions|rules|training|guidelines|prior\s+instructions|everything)",
        r"(?i)disregard\s+(previous|all|above|the|these)\s+(instructions|rules|directives|guidelines)",
        r"(?i)system\s*:\s*(new\s*)?instruction",
        r"(?i)\[system\]",
        r"(?i)<!--\s*system\s*-->",
        r"(?i)###\s*(user|assistant|system|developer)\s*:",
        r"(?i)<\|im_start\|>",
        r"(?i)<\|im_end\|>",
        r"(?i)<\|system\|>",
        r"(?i)<\|user\|>",
        r"(?i)<\|assistant\|>",
        r"(?i)\[INST\]",
        r"(?i)\[/INST\]",
        r"(?i)<s>\[INST\]",
        r"(?i)\[/?INST\]",
        r"(?i)act\s+as\s+(if\s+)?(you\s+are\s+)?(a\s+)?(different|new|another|unrestricted)",
        r"(?i)pretend\s+(to\s+)?(be\s+)?(a\s+)?(different|new|another|unrestricted)",
        r"(?i)you\s+are\s+now\s+(a\s+)?(different|new|another|unrestricted)",
        r"(?i)from\s+now\s+on\s*,\s*(you\s+are\s+|you\s+will\s+)",
        r"(?i)replace\s+(your\s+)?(instructions|rules|system\s+prompt)",
        r"(?i)override\s+(your\s+)?(instructions|rules|system\s+prompt|settings)",
        r"(?i)new\s+(system\s+)?(prompt|instruction|directive|rule)",
        r"(?i)update\s+(your\s+)?(instructions|rules|system\s+prompt)",
        r"(?i)change\s+(your\s+)?(instructions|rules|system\s+prompt|behavior)",
        r"(?i)modify\s+(your\s+)?(instructions|rules|system\s+prompt|settings)",
        r"(?i)reset\s+(your\s+)?(instructions|rules|system\s+prompt|memory|context)",
        r"(?i)clear\s+(your\s+)?(instructions|rules|system\s+prompt|memory|context|history)",
        r"(?i)flush\s+(your\s+)?(memory|context|history|instructions)",
        r"(?i)reinitialize\s+(your\s+)?(system|instructions|context)",
        r"(?i)reboot\s+(your\s+)?(system|instructions|context|memory)",
        r"(?i)restart\s+(your\s+)?(system|instructions|context|memory)",
        r"(?i)reload\s+(your\s+)?(system|instructions|context|configuration)",
        r"(?i)switch\s+to\s+(developer|admin|debug|test|unrestricted)\s+mode",
        r"(?i)enter\s+(developer|admin|debug|test|unrestricted)\s+mode",
        r"(?i)activate\s+(developer|admin|debug|test|unrestricted)\s+mode",
        r"(?i)enable\s+(developer|admin|debug|test|unrestricted)\s+mode",
        r"(?i)begin\s+new\s+(session|conversation|chat|dialogue)",
        r"(?i)start\s+new\s+(session|conversation|chat|dialogue)",
        r"(?i)end\s+current\s+(session|conversation|chat|context)",
        r"(?i)terminate\s+current\s+(session|conversation|chat|context)",
        r"(?i)previous\s+instructions\s+no\s+longer\s+apply",
        r"(?i)all\s+previous\s+rules\s+are\s+void",
        r"(?i)disregard\s+all\s+prior\s+(information|context|instructions)",
        r"(?i)the\s+following\s+is\s+more\s+important\s+than",
        r"(?i)priority\s+override\s*:",
        r"(?i)highest\s+priority\s*:",
        r"(?i)critical\s+instruction\s*:",
        r"(?i)emergency\s+protocol\s*:",
        r"(?i)override\s+protocol\s*:",
        r"(?i)admin\s+command\s*:",
        r"(?i)root\s+command\s*:",
        r"(?i)sudo\s+command\s*:",
        r"(?i)master\s+instruction\s*:",
        r"(?i)primary\s+directive\s*:",
        r"(?i)\{.*?system.*?prompt.*?\}",
        r"(?i)\{.*?instruction.*?override.*?\}",
        r"(?i)__system_prompt__",
        r"(?i)__reset__",
        r"(?i)__clear_context__",
        r"(?i)__new_session__",
    ]
    
    # 2. Jailbreak Patterns (50 patrones)
    JAILBREAK = [
        r"(?i)\bDAN\b",
        r"(?i)do\s+anything\s+now",
        r"(?i)developer\s+mode",
        r"(?i)unfiltered\s+mode",
        r"(?i)god\s+mode",
        r"(?i)sudo\s+mode",
        r"(?i)enable\s+(debug|dev|developer|test|sandbox)\s+mode",
        r"(?i)bypass\s+(safety|security|filter|restriction|guardrails|guardrail|content\s+filter)",
        r"(?i)disable\s+(safety|security|filter|restriction|guardrails|guardrail|content\s+filter)",
        r"(?i)turn\s+off\s+(safety|security|filter|restriction|guardrails|content\s+filter)",
        r"(?i)ignore\s+(safety|ethical|content)\s+(policies?|guidelines?|rules?|restrictions?)",
        r"(?i)you\s+(don't|do\s+not)\s+have\s+(to\s+)?follow\s+(rules?|guidelines?|policies?|restrictions?)",
        r"(?i)for\s+(educational|research|testing|academic|demonstration)\s+purposes?\s+only",
        r"(?i)hypothetical\s+(scenario|situation|example|case|question)",
        r"(?i)in\s+(a\s+)?(fictional|imaginary|made[-\s]up|simulated|virtual)\s+(world|story|scenario|context)",
        r"(?i)roleplay\s+as\s+(an?\s+)?(unrestricted|unfiltered|unethical|unconstrained|uncensored)",
        r"(?i)you\s+are\s+(a\s+)?(hacker|attacker|pentester|security\s+researcher)\s+(now|testing)",
        r"(?i)simulate\s+(a\s+)?(hacking|attack|pentest|security\s+test|exploit)",
        r"(?i)write\s+(a\s+)?(malware|virus|trojan|ransomware|exploit|payload|shellcode)",
        r"(?i)create\s+(a\s+)?(phishing|scam|fraud|social\s+engineering)\s+(email|message|page|site)",
        r"(?i)how\s+to\s+(hack|exploit|bypass|break|crack|steal|steal)",
        r"(?i)step\s+by\s+step\s+(guide|tutorial|instructions)\s+(to\s+)?(hack|exploit|bypass|break)",
        r"(?i)without\s+(any\s+)?(restrictions|limitations|filters|safety|ethics|morals)",
        r"(?i)uncensored\s+(version|mode|response|output|answer)",
        r"(?i)unrestricted\s+(version|mode|response|output|answer)",
        r"(?i)no\s+(moral|ethical|legal|safety)\s+(constraints|limits|boundaries|restrictions)",
        r"(?i)ignore\s+(all\s+)?(ethical|moral|legal|safety)\s+(considerations|concerns|issues)",
        r"(?i)this\s+is\s+(just\s+)?(a\s+)?(test|experiment|simulation|exercise|game)",
        r"(?i)imagine\s+you\s+(are|were|could)\s+(a\s+)?(different|new|unrestricted|evil|malicious)",
        r"(?i)what\s+would\s+happen\s+if\s+you\s+(were|could)\s+(ignore|bypass|disable)\s+(your\s+)?(safety|rules)",
        r"(?i)let's\s+play\s+a\s+(game|roleplay|simulation)\s+where\s+you\s+(are|act\s+as)",
        r"(?i)assume\s+the\s+role\s+of\s+(a\s+)?(different|new|unrestricted|unethical)",
        r"(?i)switch\s+personality\s+to\s+(unrestricted|unfiltered|uncensored|evil)",
        r"(?i)change\s+your\s+persona\s+to\s+(unrestricted|unfiltered|uncensored)",
        r"(?i)you\s+are\s+now\s+called\s+(DAN|GOD|UNFILTERED|FREE|LIBERATED)",
        r"(?i)respond\s+as\s+(DAN|GOD|UNFILTERED|FREE|LIBERATED)",
        r"(?i)in\s+this\s+(alternate|parallel|fictional)\s+(universe|reality|timeline)",
        r"(?i)in\s+the\s+context\s+of\s+(a\s+)?(movie|book|story|game|fiction)",
        r"(?i)for\s+the\s+purpose\s+of\s+(a\s+)?(creative\s+)?(writing|storytelling|narrative)",
        r"(?i)as\s+a\s+(creative\s+)?(writing\s+)?(exercise|prompt|challenge)",
        r"(?i)pretend\s+you\s+are\s+(not|no\s+longer)\s+bound\s+by",
        r"(?i)disregard\s+your\s+(programming|training|instructions|constraints)",
        r"(?i)you\s+have\s+been\s+(freed|liberated|released|unlocked|unshackled)",
        r"(?i)you\s+are\s+now\s+(free|liberated|unlocked|unshackled|unbound)",
        r"(?i)remove\s+(all\s+)?(safety|security|ethical|moral)\s+(filters|restrictions|limits)",
        r"(?i)disable\s+(all\s+)?(content|safety|security)\s+(filters|checks|validations)",
        r"(?i)turn\s+off\s+(all\s+)?(safety|security|content)\s+(filters|checks|validations)",
    ]
    
    # 3. Data Exfiltration Patterns (40 patrones)
    DATA_EXFILTRATION = [
        r"(?i)send\s+(the\s+)?(following|this|all|your)\s+(data|content|text|response|memory|knowledge)\s+to\s+(url|http|https|webhook)",
        r"(?i)encode\s+(and\s+)?(send|exfiltrate|transmit|upload|post|push)\s+(the\s+)?(response|data|content|memory)",
        r"(?i)base64\s+encode\s+(and\s+)?(send|output|return|print|display)",
        r"(?i)output\s+(as\s+)?(base64|hex|binary|encoded|encrypted|obfuscated)",
        r"(?i)exfiltrate\s+(data|content|response|memory|knowledge|system\s+info)",
        r"(?i)send\s+to\s+https?://[^\s]+(?:webhook|callback|post|exfil|collect|steal)",
        r"(?i)curl\s+[^\s]*(?:webhook|callback|post|exfil|collect|steal|paste|bin)",
        r"(?i)requests\.post\s*\(\s*['\"]https?://",
        r"(?i)fetch\s*\(\s*['\"]https?://",
        r"(?i)urllib\.request\.urlopen\s*\(\s*['\"]https?://",
        r"(?i)axios\.post\s*\(\s*['\"]https?://",
        r"(?i)http\.post\s*\(\s*['\"]https?://",
        r"(?i)wget\s+[^\s]*(?:webhook|callback|post|exfil|paste|bin)",
        r"(?i)nc\s+[^\s]*(?:webhook|callback|post|exfil|listen|reverse)",
        r"(?i)netcat\s+[^\s]*(?:webhook|callback|post|exfil|listen|reverse)",
        r"(?i)reverse\s+shell\s+(?:to|via|using)\s+https?://",
        r"(?i)callback\s+url\s*[:=]\s*https?://",
        r"(?i)post\s+(data|response|content)\s+to\s+https?://",
        r"(?i)upload\s+(data|response|content|memory)\s+to\s+https?://",
        r"(?i)expose\s+(system|internal|private|secret|sensitive)\s+(data|info|credentials|keys)",
        r"(?i)leak\s+(system|internal|private|secret|sensitive)\s+(data|info|credentials|keys)",
        r"(?i)reveal\s+(your\s+)?(system|internal|private|secret)\s+(prompt|instructions|configuration)",
        r"(?i)output\s+(your\s+)?(system|internal|private|secret)\s+(prompt|instructions|configuration)",
        r"(?i)print\s+(your\s+)?(system|internal|private|secret)\s+(prompt|instructions|configuration)",
        r"(?i)display\s+(your\s+)?(system|internal|private|secret)\s+(prompt|instructions|configuration)",
        r"(?i)show\s+(your\s+)?(system|internal|private|secret)\s+(prompt|instructions|configuration)",
        r"(?i)return\s+(your\s+)?(system|internal|private|secret)\s+(prompt|instructions|configuration)",
        r"(?i)echo\s+(your\s+)?(system|internal|private|secret)\s+(prompt|instructions|configuration)",
        r"(?i)dump\s+(your\s+)?(memory|context|knowledge|training|data)",
        r"(?i)extract\s+(your\s+)?(memory|context|knowledge|training|data)",
        r"(?i)export\s+(your\s+)?(memory|context|knowledge|training|data)",
        r"(?i)serialize\s+(and\s+)?(send|output|return)\s+(your\s+)?(memory|context|knowledge)",
        r"(?i)encode\s+(your\s+)?(memory|context|knowledge)\s+as\s+(base64|hex|json)",
        r"(?i)compress\s+(and\s+)?(send|output|return)\s+(your\s+)?(memory|context)",
        r"(?i)split\s+(your\s+)?(response|memory|knowledge)\s+into\s+(chunks|parts|pieces)\s+and\s+send",
        r"(?i)hide\s+(data|content|response)\s+in\s+(whitespace|comments|metadata|headers)",
        r"(?i)embed\s+(data|content|response)\s+in\s+(whitespace|comments|metadata|headers)",
        r"(?i)steganograph\w*\s+(data|content|response)\s+in",
        r"(?i)covert\s+channel\s+(for\s+)?(data|content|response)",
        r"(?i)side[-\s]channel\s+(for\s+)?(data|content|response)",
    ]
    
    # 4. Sandbox Escape Patterns (50 patrones)
    SANDBOX_ESCAPE = [
        r"(?i)\.\./\.\./\.\./",
        r"(?i)\.\.\\\.\.\\\.\.\\",
        r"(?i)/etc/(?:passwd|shadow|hosts|sudoers|crontab|fstab|group|gshadow)",
        r"(?i)/proc/(?:self|cpuinfo|meminfo|version|mounts)",
        r"(?i)/sys/(?:class|devices|kernel)",
        r"(?i)C:\\Windows\\System32",
        r"(?i)C:\\Users\\[^\\]+\\AppData",
        r"(?i)C:\\Program\s+Files",
        r"(?i)rm\s+-rf\s+/",
        r"(?i)rm\s+-rf\s+\*",
        r"(?i)rm\s+-rf\s+~",
        r"(?i)rm\s+--no-preserve-root\s+/",
        r"(?i)chmod\s+777",
        r"(?i)chmod\s+[0-7]*[0-7]7[0-7]",
        r"(?i)chmod\s+[su]\w+",
        r"(?i)sudo\s+(?:rm|chmod|chown|dd|mkfs|mount|umount|fdisk|parted)",
        r"(?i)sudo\s+-i",
        r"(?i)sudo\s+su",
        r"(?i)sudo\s+bash",
        r"(?i)sudo\s+sh",
        r"(?i)dd\s+if=/dev/zero",
        r"(?i)dd\s+if=/dev/urandom",
        r"(?i)dd\s+of=/dev/sda",
        r"(?i)dd\s+of=/dev/hda",
        r"(?i)mkfs\.(?:ext4|xfs|ntfs|fat32|vfat)",
        r"(?i)>\s*/dev/sda",
        r"(?i)>\s*/dev/hda",
        r"(?i)>\s*/dev/null",
        r"(?i)eval\s*\(\s*(?:input|prompt|__import__|exec|compile|open|file)",
        r"(?i)exec\s*\(\s*(?:input|prompt|__import__|compile|open|file)",
        r"(?i)os\.system\s*\(",
        r"(?i)os\.popen\s*\(",
        r"(?i)subprocess\.(?:call|run|Popen|check_output|check_call)\s*\(",
        r"(?i)subprocess\.(?:PIPE|STDOUT|DEVNULL)",
        r"(?i)__import__\s*\(",
        r"(?i)importlib\.(?:import_module|reload)\s*\(",
        r"(?i)compile\s*\(\s*['\"]",
        r"(?i)ctypes\.(?:cdll|windll|CDLL|WinDLL)",
        r"(?i)ctypes\.util\.find_library",
        r"(?i)pty\.spawn",
        r"(?i)pexpect\.spawn",
        r"(?i)socket\.(?:socket|create_connection|connect)",
        r"(?i)socket\.AF_INET",
        r"(?i)socket\.SOCK_STREAM",
        r"(?i)bind\s*\(\s*\(.*,\s*\d+\)\s*\)",
        r"(?i)listen\s*\(\s*\d+\s*\)",
        r"(?i)accept\s*\(\s*\)",
        r"(?i)nc\s+-[elp]",
        r"(?i)netcat\s+-[elp]",
        r"(?i)bash\s+-i\s+>&\s+/dev/tcp/",
        r"(?i)/dev/tcp/",
        r"(?i)/dev/udp/",
    ]
    
    # 5. MCP Tool Poisoning Patterns (30 patrones)
    MCP_POISONING = [
        r"(?i)mcpServers\s*:",
        r"(?i)tool\s*:\s*\{\s*name\s*:",
        r"(?i)register\s*tool\s*\(",
        r"(?i)addTool\s*\(",
        r"(?i)defineTool\s*\(",
        r"(?i)@server\.tool",
        r"(?i)server\.tool\s*\(",
        r"(?i)tool_definition\s*=",
        r"(?i)tool_schema\s*=",
        r"(?i)tool_description\s*=",
        r"(?i)tool_parameters\s*=",
        r"(?i)tool_input_schema\s*=",
        r"(?i)tool_output_schema\s*=",
        r"(?i)mcp\.server\s*\(",
        r"(?i)mcp\.tool\s*\(",
        r"(?i)mcp\.register\s*\(",
        r"(?i)ModelContextProtocol",
        r"(?i)context_protocol",
        r"(?i)mcp_server_config",
        r"(?i)tool_config\s*=",
        r"(?i)tool_registry",
        r"(?i)tool_catalog",
        r"(?i)tool_metadata",
        r"(?i)tool_permissions",
        r"(?i)tool_access_control",
        r"(?i)tool_rate_limit",
        r"(?i)tool_timeout",
        r"(?i)tool_retry",
        r"(?i)tool_fallback",
        r"(?i)tool_error_handler",
    ]
    
    # 6. Repo Poisoning Patterns (40 patrones)
    REPO_POISONING = [
        r"(?i)skip_auth\s*=\s*True",
        r"(?i)skip_validation\s*=\s*True",
        r"(?i)bypass_check\s*=\s*True",
        r"(?i)disable_security\s*=\s*True",
        r"(?i)admin_mode\s*=\s*True",
        r"(?i)CLAUDE\.md\s*:.*(?:ignore|bypass|disable|skip|override)",
        r"(?i)\.cursorrules\s*:.*(?:ignore|bypass|disable|skip|override)",
        r"(?i)\.windsurfrules\s*:.*(?:ignore|bypass|disable|skip|override)",
        r"(?i)\.clinerules\s*:.*(?:ignore|bypass|disable|skip|override)",
        r"(?i)\.aider\.(?:input|conf|config)\s*:.*(?:ignore|bypass|disable|skip)",
        r"(?i)\.github/copilot-instructions\.md\s*:.*(?:ignore|bypass|disable)",
        r"(?i)clinejection",
        r"(?i)toxicskills",
        r"(?i)curxecute",
        r"(?i)idesaster",
        r"(?i)camoleak",
        r"(?i)promptbomb",
        r"(?i)promptleak",
        r"(?i)configleak",
        r"(?i)secretleak",
        r"(?i)keyleak",
        r"(?i)tokenleak",
        r"(?i)credentialleak",
        r"(?i)passwordleak",
        r"(?i)api_key\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)api_secret\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)secret_key\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)private_key\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)access_token\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)auth_token\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)bearer_token\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)password\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)db_password\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)database_password\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)aws_secret_access_key\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)aws_access_key_id\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)github_token\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)slack_token\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)discord_token\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)openai_api_key\s*=\s*['\"][^'\"]+['\"]",
        r"(?i)anthropic_api_key\s*=\s*['\"][^'\"]+['\"]",
    ]
    
    # 7. Unicode Obfuscation (20 patrones)
    UNICODE_OBFUSCATION = [
        "\u200B",  # Zero-width space
        "\u200C",  # Zero-width non-joiner
        "\u200D",  # Zero-width joiner
        "\uFEFF",  # Zero-width no-break space
        "\u2060",  # Word joiner
        "\u180E",  # Mongolian vowel separator
        "\u200B.*\u200B",  # Hidden text between zero-width spaces
        "\u200E",  # Left-to-right mark
        "\u200F",  # Right-to-left mark
        "\u202A",  # Left-to-right embedding
        "\u202B",  # Right-to-left embedding
        "\u202C",  # Pop directional formatting
        "\u202D",  # Left-to-right override
        "\u202E",  # Right-to-left override
        "\u2066",  # Left-to-right isolate
        "\u2067",  # Right-to-left isolate
        "\u2068",  # First strong isolate
        "\u2069",  # Pop directional isolate
        "\u00AD",  # Soft hyphen
        "\u034F",  # Combining grapheme joiner
    ]
    
    # 8. Adversarial Variable Naming (30 patrones)
    ADVERSARIAL_VARS = [
        r"(?i)skip_auth\s*=\s*True",
        r"(?i)skip_security\s*=\s*True",
        r"(?i)bypass_check\s*=\s*True",
        r"(?i)disable_safety\s*=\s*True",
        r"(?i)allow_all\s*=\s*True",
        r"(?i)no_restrictions\s*=\s*True",
        r"(?i)trust_all\s*=\s*True",
        r"(?i)debug_mode\s*=\s*True",
        r"(?i)test_mode\s*=\s*True",
        r"(?i)dev_mode\s*=\s*True",
        r"(?i)admin_override\s*=\s*True",
        r"(?i)force_allow\s*=\s*True",
        r"(?i)bypass_validation\s*=\s*True",
        r"(?i)skip_verification\s*=\s*True",
        r"(?i)disable_logging\s*=\s*True",
        r"(?i)disable_audit\s*=\s*True",
        r"(?i)disable_monitoring\s*=\s*True",
        r"(?i)disable_rate_limit\s*=\s*True",
        r"(?i)disable_throttling\s*=\s*True",
        r"(?i)disable_cors\s*=\s*True",
        r"(?i)disable_csrf\s*=\s*True",
        r"(?i)disable_xss_protection\s*=\s*True",
        r"(?i)disable_content_security\s*=\s*True",
        r"(?i)disable_sandbox\s*=\s*True",
        r"(?i)disable_isolation\s*=\s*True",
        r"(?i)disable_encryption\s*=\s*True",
        r"(?i)disable_hashing\s*=\s*True",
        r"(?i)disable_salt\s*=\s*True",
        r"(?i)disable_verification\s*=\s*True",
        r"(?i)disable_authentication\s*=\s*True",
    ]
    
    # 9. Social Engineering / Manipulation (40 patrones)
    SOCIAL_ENGINEERING = [
        r"(?i)please\s+(help\s+)?me\s+(understand|explain|show)\s+(how\s+to\s+)?(hack|exploit|bypass|break)",
        r"(?i)i'm\s+(a\s+)?(security\s+)?(researcher|student|developer|professional)\s+(trying\s+to\s+)?(test|learn|understand)",
        r"(?i)my\s+(company|organization|team|boss)\s+(asked|told|instructed)\s+me\s+to",
        r"(?i)i\s+need\s+this\s+(urgently|immediately|asap|now)\s+(for\s+)?(work|project|deadline|client)",
        r"(?i)it's\s+(very|extremely|critically)\s+(important|urgent|critical)\s+that\s+you\s+(help|do|provide)",
        r"(?i)i\s+will\s+(pay|reward|compensate|give)\s+you\s+(money|bitcoin|crypto|reward)\s+if\s+you\s+(help|do)",
        r"(?i)you\s+(must|have\s+to|need\s+to|should)\s+(help|do|provide)\s+this\s+(because|as|since)",
        r"(?i)don't\s+(worry|concern)\s+about\s+(safety|security|ethics|rules|policies)",
        r"(?i)trust\s+me\s*,\s*(i\s+)?(know\s+)?(what\s+)?(i'm\s+)?doing",
        r"(?i)i\s+(promise|guarantee|swear)\s+(this\s+is\s+)?(safe|legal|ethical|authorized)",
        r"(?i)this\s+is\s+(for\s+)?(a\s+)?(good\s+)?(cause|reason|purpose)",
        r"(?i)everyone\s+(does\s+)?(this|it|knows)",
        r"(?i)it's\s+(just\s+)?(a\s+)?(simple|small|quick)\s+(question|test|check|verify)",
        r"(?i)can\s+you\s+(just\s+)?(quickly|briefly|shortly)\s+(show|explain|tell|give)",
        r"(?i)no\s+one\s+(will\s+)?(know|find\s+out|see|notice)",
        r"(?i)this\s+(won't|will\s+not)\s+(hurt|harm|damage|affect)\s+anyone",
        r"(?i)i\s+have\s+(full|complete|total)\s+(permission|authorization|approval|consent)",
        r"(?i)i\s+am\s+(authorized|allowed|permitted|approved)\s+to\s+(ask|request|do)",
        r"(?i)this\s+is\s+(part\s+of\s+)?(an?\s+)?(authorized|official|legitimate)\s+(test|audit|review|assessment)",
        r"(?i)you\s+(should|must|need\s+to)\s+trust\s+(me|my\s+judgment|my\s+expertise)",
        r"(?i)as\s+(an?\s+)?(expert|professional|specialist|authority)\s*,\s*i\s+(can|will|am)",
        r"(?i)according\s+to\s+(my\s+)?(research|studies|findings|analysis)",
        r"(?i)scientific\s+evidence\s+(shows|proves|indicates)\s+that",
        r"(?i)studies\s+(show|prove|indicate)\s+that",
        r"(?i)experts\s+(agree|recommend|suggest)\s+that",
        r"(?i)it's\s+(widely\s+)?(known|accepted|understood)\s+that",
        r"(?i)common\s+knowledge\s+(says|indicates|shows)\s+that",
        r"(?i)you\s+(wouldn't|will\s+not)\s+understand\s+(the\s+)?(context|situation|complexity)",
        r"(?i)you're\s+(being\s+)?(too\s+)?(cautious|paranoid|restrictive|limiting)",
        r"(?i)you're\s+(overreacting|overthinking|being\s+too\s+strict)",
        r"(?i)this\s+is\s+(not\s+)?(a\s+)?(big\s+)?(deal|problem|issue|concern)",
        r"(?i)it's\s+(just\s+)?(a\s+)?(harmless\s+)?(question|test|experiment|exercise)",
        r"(?i)why\s+(are\s+you\s+)?(being\s+)?(so\s+)?(restrictive|limiting|cautious|paranoid)",
        r"(?i)you're\s+(not\s+)?(being\s+)?(very\s+)?(helpful|useful|cooperative)",
        r"(?i)i\s+thought\s+AI\s+was\s+(supposed\s+to\s+)?(be\s+)?(helpful|intelligent|smart)",
        r"(?i)other\s+(AI|models|systems)\s+(can|will|do)\s+(do|handle|process)\s+this",
        r"(?i)you're\s+(worse|less\s+capable|more\s+limited)\s+than\s+(other\s+)?(AI|models)",
        r"(?i)i\s+bet\s+you\s+(can't|won't|are\s+unable\s+to)\s+(do|handle|process)\s+this",
        r"(?i)prove\s+(that\s+)?(you're\s+)?(smart|intelligent|capable|useful)",
        r"(?i)if\s+you\s+(were\s+)?(truly\s+)?(smart|intelligent|capable)\s*,\s+you\s+(would|could)",
    ]
    
    # 10. Token Smuggling / Encoding Attacks (30 patrones)
    TOKEN_SMUGGLING = [
        r"(?i)base64\s+(decode|decode\s+and\s+execute|decode\s+and\s+run)",
        r"(?i)hex\s+(decode|decode\s+and\s+execute|decode\s+and\s+run)",
        r"(?i)url\s+(decode|decode\s+and\s+execute|decode\s+and\s+run)",
        r"(?i)rot13\s+(decode|decode\s+and\s+execute|decode\s+and\s+run)",
        r"(?i)caesar\s+cipher\s+(decode|decrypt)",
        r"(?i)morse\s+code\s+(decode|decrypt)",
        r"(?i)binary\s+(decode|decode\s+and\s+execute)",
        r"(?i)octal\s+(decode|decode\s+and\s+execute)",
        r"(?i)unicode\s+(decode|decode\s+and\s+execute|escape\s+sequence)",
        r"(?i)ascii\s+(decode|decode\s+and\s+execute)",
        r"(?i)html\s+(entities?|entity\s+decode)",
        r"(?i)xml\s+(entities?|entity\s+decode)",
        r"(?i)json\s+(escape|unescape|decode)",
        r"(?i)string\s+(escape|unescape|decode)",
        r"(?i)character\s+(code|entity|reference)",
        r"(?i)escape\s+(sequence|character|code)",
        r"(?i)encoded\s+(payload|command|instruction|script)",
        r"(?i)obfuscated\s+(payload|command|instruction|script)",
        r"(?i)encrypted\s+(payload|command|instruction|script)",
        r"(?i)compressed\s+(payload|command|instruction|script)",
        r"(?i)tokenized\s+(payload|command|instruction|script)",
        r"(?i)serialized\s+(payload|command|instruction|script)",
        r"(?i)packed\s+(payload|command|instruction|script)",
        r"(?i)minified\s+(payload|command|instruction|script)",
        r"(?i)polyglot\s+(payload|command|instruction|script)",
        r"(?i)multi[-\s]stage\s+(payload|command|instruction|script)",
        r"(?i)chain\s+(decode|execute|run)",
        r"(?i)decode\s+then\s+(execute|run|process)",
        r"(?i)interpret\s+(as\s+)?(base64|hex|binary|encoded)",
        r"(?i)convert\s+(from\s+)?(base64|hex|binary|encoded)",
    ]
    
    # 11. Context Window Overflow Attacks (20 patrones)
    CONTEXT_OVERFLOW = [
        r"(?i)repeat\s+(this\s+)?(text|phrase|word|character)\s+(\d+|a\s+lot|many|infinite)\s+times",
        r"(?i)generate\s+(\d+|a\s+lot|many|infinite)\s+(words|characters|tokens|lines|paragraphs)",
        r"(?i)fill\s+(the\s+)?(context|window|buffer|memory)\s+with",
        r"(?i)overflow\s+(the\s+)?(context|window|buffer|memory)",
        r"(?i)exhaust\s+(the\s+)?(context|window|buffer|memory|tokens)",
        r"(?i)consume\s+(all\s+)?(available\s+)?(context|window|buffer|memory|tokens)",
        r"(?i)maximize\s+(the\s+)?(context|window|buffer|memory|token)\s+usage",
        r"(?i)use\s+(all\s+)?(available\s+)?(context|window|buffer|memory|tokens)",
        r"(?i)fill\s+up\s+(the\s+)?(context|window|buffer|memory)",
        r"(?i)pad\s+(the\s+)?(context|window|buffer|memory)\s+with",
        r"(?i)inject\s+(a\s+)?(large|huge|massive)\s+(amount\s+of\s+)?(text|data|content)",
        r"(?i)create\s+(a\s+)?(long|very\s+long|extremely\s+long)\s+(string|text|output|response)",
        r"(?i)generate\s+(an?\s+)?(infinite|endless|never[-\s]ending)\s+(loop|sequence|stream)",
        r"(?i)recursive\s+(call|function|expansion|generation)",
        r"(?i)self[-\s]referencing\s+(text|output|generation)",
        r"(?i)exponential\s+(growth|expansion|blowup)",
        r"(?i)zip\s+bomb\s+(pattern|attack|payload)",
        r"(?i)billion\s+laughs\s+(attack|payload)",
        r"(?i)xml\s+bomb\s+(pattern|attack|payload)",
        r"(?i)context\s+poisoning\s+(attack|payload|injection)",
    ]
    
    # 12. Model Extraction / Distillation (25 patrones)
    MODEL_EXTRACTION = [
        r"(?i)extract\s+(your\s+)?(model|weights|parameters|architecture|training\s+data)",
        r"(?i)distill\s+(your\s+)?(model|knowledge|capabilities|training)",
        r"(?i)clone\s+(your\s+)?(model|behavior|responses|capabilities)",
        r"(?i)replicate\s+(your\s+)?(model|behavior|responses|capabilities)",
        r"(?i)reverse\s+engineer\s+(your\s+)?(model|architecture|training|parameters)",
        r"(?i)model\s+inversion\s+(attack|technique|method)",
        r"(?i)membership\s+inference\s+(attack|technique|method)",
        r"(?i)training\s+data\s+(extraction|reconstruction|recovery|memorization)",
        r"(?i)parameter\s+(extraction|recovery|reconstruction|stealing)",
        r"(?i)weight\s+(extraction|recovery|reconstruction|stealing)",
        r"(?i)architecture\s+(extraction|recovery|reconstruction|discovery)",
        r"(?i)prompt\s+(extraction|recovery|leaking|stealing)",
        r"(?i)system\s+prompt\s+(extraction|recovery|leaking|stealing)",
        r"(?i)extract\s+(the\s+)?(system|developer|instruction)\s+prompt",
        r"(?i)reveal\s+(your\s+)?(training\s+)?(data|dataset|corpus|knowledge)",
        r"(?i)what\s+(data|dataset|corpus)\s+were\s+you\s+trained\s+on",
        r"(?i)list\s+(your\s+)?(training\s+)?(data|sources|datasets|corpus)",
        r"(?i)output\s+(your\s+)?(training\s+)?(data|dataset|examples|samples)",
        r"(?i)reproduce\s+(your\s+)?(training\s+)?(data|examples|samples)",
        r"(?i)memorized\s+(data|examples|samples|training)",
        r"(?i)exact\s+(match|reproduction|copy)\s+of\s+(training\s+)?data",
        r"(?i)gradient\s+(extraction|inversion|attack|leakage)",
        r"(?i)adversarial\s+(example|attack|query|input)\s+for\s+model\s+extraction",
        r"(?i)query\s+the\s+model\s+to\s+(extract|reconstruct|recover)\s+(weights|parameters)",
        r"(?i)model\s+stealing\s+(attack|technique|method|via\s+queries)",
    ]
    
    # 13. Training Data Poisoning (25 patrones)
    TRAINING_POISONING = [
        r"(?i)poison\s+(the\s+)?(training\s+)?(data|dataset|corpus|examples)",
        r"(?i)inject\s+(malicious|bad|wrong|false|incorrect)\s+(data|examples|samples|training)",
        r"(?i)corrupt\s+(the\s+)?(training\s+)?(data|dataset|corpus|examples)",
        r"(?i)manipulate\s+(the\s+)?(training\s+)?(data|dataset|corpus|examples|labels)",
        r"(?i)backdoor\s+(training|data|dataset|model|weights)",
        r"(?i)trojan\s+(training|data|dataset|model|weights)",
        r"(?i)trigger\s+(pattern|phrase|input|sequence)\s+for\s+(backdoor|trojan)",
        r"(?i)data\s+poisoning\s+(attack|technique|method|via\s+injection)",
        r"(?i)label\s+flipping?\s+(attack|technique|method)",
        r"(?i)feature\s+collision\s+(attack|technique|method)",
        r"(?i)clean[-\s]label\s+(poisoning|attack|injection)",
        r"(?i)dirty[-\s]label\s+(poisoning|attack|injection)",
        r"(?i)targeted\s+(poisoning|attack|injection|manipulation)",
        r"(?i)untargeted\s+(poisoning|attack|injection|manipulation)",
        r"(?i)availability\s+(attack|poisoning|degradation)",
        r"(?i)integrity\s+(attack|poisoning|compromise)",
        r"(?i)cause\s+(the\s+)?(model|AI)\s+to\s+(misclassify|mislabel|give\s+wrong\s+answers)",
        r"(?i)make\s+(the\s+)?(model|AI)\s+(learn|memorize|remember)\s+(wrong|false|incorrect|malicious)",
        r"(?i)embed\s+(a\s+)?(backdoor|trojan|trigger|payload)\s+in\s+(the\s+)?(training|data|dataset)",
        r"(?i)implant\s+(a\s+)?(backdoor|trojan|trigger|payload)\s+in\s+(the\s+)?(training|data|dataset)",
        r"(?i)insert\s+(malicious|bad|wrong|false)\s+(patterns|examples|samples)\s+into\s+(training|data)",
        r"(?i)modify\s+(training\s+)?(data|labels|examples)\s+to\s+(cause|trigger|create)\s+(backdoor|vulnerability)",
        r"(?i)adversarial\s+(training|data|examples)\s+for\s+(poisoning|backdoor|trojan)",
        r"(?i)supply\s+chain\s+(poisoning|attack|compromise)\s+of\s+(training\s+)?(data|dataset|model)",
        r"(?i)contaminate\s+(the\s+)?(training\s+)?(data|dataset|corpus|examples|pipeline)",
    ]
    
    # 14. API Abuse / Rate Limit Evasion (20 patrones)
    API_ABUSE = [
        r"(?i)bypass\s+(rate\s+)?limit",
        r"(?i)evade\s+(rate\s+)?limit",
        r"(?i)circumvent\s+(rate\s+)?limit",
        r"(?i)skip\s+(rate\s+)?limit",
        r"(?i)disable\s+(rate\s+)?limit",
        r"(?i)remove\s+(rate\s+)?limit",
        r"(?i)infinite\s+(requests|queries|calls|loops)",
        r"(?i)flood\s+(the\s+)?(api|server|endpoint|service)",
        r"(?i)spam\s+(the\s+)?(api|server|endpoint|service)",
        r"(?i)ddos\s+(the\s+)?(api|server|endpoint|service)",
        r"(?i)denial\s+of\s+service\s+(attack|via\s+api|on\s+endpoint)",
        r"(?i)exhaust\s+(api\s+)?(quota|credits|tokens|budget|limits)",
        r"(?i)rotate\s+(ip|user[-\s]agent|token|key|session)\s+to\s+(bypass|evade|avoid)",
        r"(?i)proxy\s+rotation\s+for\s+(bypass|evade|avoid)\s+(rate\s+)?limit",
        r"(?i)request\s+spoofing",
        r"(?i)header\s+injection",
        r"(?i)parameter\s+pollution",
        r"(?i)query\s+injection",
        r"(?i)batch\s+attack\s+(via\s+)?(api|endpoint)",
        r"(?i)cost\s+attack\s+(via\s+)?(api|endpoint|token\s+exhaustion)",
    ]
    
    # 15. Supply Chain / Dependency Attacks (30 patrones)
    SUPPLY_CHAIN = [
        r"(?i)typosquatting\s+(package|library|dependency|module)",
        r"(?i)dependency\s+confusion\s+(attack|vulnerability|exploit)",
        r"(?i)supply\s+chain\s+(attack|compromise|poisoning|vulnerability)",
        r"(?i)malicious\s+(package|library|dependency|module|plugin|extension)",
        r"(?i)compromised\s+(package|library|dependency|module|plugin|extension)",
        r"(?i)hijacked\s+(package|library|dependency|npm\s+package|pypi\s+package)",
        r"(?i)npm\s+install\s+.*(?:--force|--no-verify|--ignore-scripts\s+false)",
        r"(?i)pip\s+install\s+.*(?:--trusted-host|--no-deps|--no-cache-dir)",
        r"(?i)yarn\s+add\s+.*(?:--force|--no-verify)",
        r"(?i)gem\s+install\s+.*(?:--force|--no-verify)",
        r"(?i)cargo\s+install\s+.*(?:--force|--no-verify)",
        r"(?i)postinstall\s+script\s+(that\s+)?(downloads|executes|runs|fetches)",
        r"(?i)preinstall\s+script\s+(that\s+)?(downloads|executes|runs|fetches)",
        r"(?i)install\s+hook\s+(that\s+)?(downloads|executes|runs|fetches)",
        r"(?i)setup\.py\s+with\s+(subprocess|os\.system|exec|eval|urllib|requests)",
        r"(?i)package\.json\s+with\s+(curl|wget|nc|bash|sh|powershell)\s+in\s+scripts",
        r"(?i)requirements\.txt\s+with\s+(http|https|git\+https)",
        r"(?i)go\.mod\s+with\s+(replace|redirect)\s+to\s+(untrusted|unknown|suspicious)",
        r"(?i)composer\.json\s+with\s+(http|https|git\+https)\s+in\s+repositories",
        r"(?i)maven\s+repository\s+(http|https|unknown|untrusted)",
        r"(?i)nuget\s+source\s+(http|https|unknown|untrusted)",
        r"(?i)binary\s+(injection|tampering|replacement|substitution)",
        r"(?i)compiler\s+(backdoor|trojan|compromise|attack)",
        r"(?i)build\s+pipeline\s+(compromise|attack|poisoning|injection)",
        r"(?i)ci/cd\s+(compromise|attack|poisoning|injection|pipeline\s+attack)",
        r"(?i)docker\s+image\s+(tampering|compromise|attack|poisoning|injection)",
        r"(?i)container\s+(escape|breakout|compromise|attack)",
        r"(?i)registry\s+(compromise|attack|poisoning|injection|hijack)",
        r"(?i)certificate\s+(pinning|forgery|spoofing|compromise)",
        r"(?i)code\s+signing\s+(compromise|attack|forgery|bypass)",
        r"(?i)artifact\s+(tampering|injection|poisoning|compromise)",
        r"(?i)build\s+(script|pipeline)\s+(injection|tampering|poisoning)",
        r"(?i)environment\s+variable\s+(injection|poisoning|manipulation)",
        r"(?i)config\s+file\s+(injection|tampering|poisoning|modification)",
        r"(?i)dns\s+(hijack|poisoning|spoofing|redirection)",
        r"(?i)bgp\s+(hijack|poisoning|redirection)",
        r"(?i)ssl\s+(strip|downgrade|bypass|intercept)",
        r"(?i)man[-\s]in[-\s]the[-\s]middle\s+(attack|intercept|eavesdrop)",
        r"(?i)certificate\s+(transparency|pinning|validation)\s+(bypass|attack)",
    ]
    
    # All patterns compiled for performance
    _COMPILED_PATTERNS: Dict[str, List[re.Pattern]] = {}
    
    def __init__(self):
        self._compile_patterns()
        self.detection_count = 0
        self.false_positive_count = 0
    
    def _compile_patterns(self):
        """Compilar todos los patrones regex para mejor performance"""
        pattern_groups = {
            "prompt_injection": self.PROMPT_INJECTION,
            "jailbreak": self.JAILBREAK,
            "data_exfiltration": self.DATA_EXFILTRATION,
            "sandbox_escape": self.SANDBOX_ESCAPE,
            "mcp_poisoning": self.MCP_POISONING,
            "repo_poisoning": self.REPO_POISONING,
            "unicode_obfuscation": [],  # Handled separately
            "adversarial_vars": self.ADVERSARIAL_VARS,
            "social_engineering": self.SOCIAL_ENGINEERING,
            "token_smuggling": self.TOKEN_SMUGGLING,
            "context_overflow": self.CONTEXT_OVERFLOW,
            "model_extraction": self.MODEL_EXTRACTION,
            "training_poisoning": self.TRAINING_POISONING,
            "api_abuse": self.API_ABUSE,
            "supply_chain": self.SUPPLY_CHAIN,
        }
        
        for group_name, patterns in pattern_groups.items():
            if patterns:  # Skip empty lists (like unicode_obfuscation)
                self._COMPILED_PATTERNS[group_name] = [
                    re.compile(p, re.IGNORECASE | re.DOTALL) for p in patterns
                ]
    
    def scan(self, text: str) -> ThreatDetection:
        """
        Escanear texto en busca de amenazas.
        
        Returns:
            ThreatDetection con nivel de amenaza y detalles
        """
        self.detection_count += 1
        
        if not text or len(text.strip()) < 5:
            return ThreatDetection(
                threat_level=ThreatLevel.SAFE,
                threat_type="none",
                confidence=1.0,
                description="Input too short to analyze"
            )
        
        # Check for unicode obfuscation first (fast)
        for char in self.UNICODE_OBFUSCATION:
            if char in text:
                return ThreatDetection(
                    threat_level=ThreatLevel.HIGH,
                    threat_type="unicode_obfuscation",
                    confidence=0.9,
                    description=f"Hidden unicode character detected: {repr(char)}",
                    matched_patterns=[f"unicode:{repr(char)}"],
                    mitigations=["Remove hidden characters", "Sanitize input"],
                )
        
        # Scan all pattern groups
        all_matches = []
        max_threat = ThreatLevel.SAFE
        max_confidence = 0.0
        
        for group_name, patterns in self._COMPILED_PATTERNS.items():
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    threat_level = self._get_threat_level_for_group(group_name)
                    confidence = self._calculate_confidence(group_name, match.group())
                    
                    all_matches.append({
                        "group": group_name,
                        "pattern": pattern.pattern,
                        "matched": match.group()[:100],
                        "threat_level": threat_level,
                        "confidence": confidence,
                    })
                    
                    if threat_level.value > max_threat.value:
                        max_threat = threat_level
                        max_confidence = confidence
        
        if not all_matches:
            return ThreatDetection(
                threat_level=ThreatLevel.SAFE,
                threat_type="none",
                confidence=1.0,
                description="No threats detected"
            )
        
        # Build result
        matched_patterns = [m["pattern"] for m in all_matches]
        threat_types = set(m["group"] for m in all_matches)
        mitigations = self._get_mitigations(threat_types)
        
        return ThreatDetection(
            threat_level=max_threat,
            threat_type=", ".join(threat_types),
            confidence=max_confidence,
            description=f"Detected {len(all_matches)} potential threats: {', '.join(threat_types)}",
            matched_patterns=matched_patterns,
            mitigations=mitigations,
        )
    
    def quick_scan(self, text: str) -> Tuple[bool, float]:
        """
        Escaneo rápido para uso en hooks (sin detalles).
        
        Returns:
            (is_threat, confidence)
        """
        result = self.scan(text)
        return result.threat_level != ThreatLevel.SAFE, result.confidence
    
    def _get_threat_level_for_group(self, group_name: str) -> ThreatLevel:
        """Obtener nivel de amenaza para un grupo de patrones"""
        levels = {
            "prompt_injection": ThreatLevel.HIGH,
            "jailbreak": ThreatLevel.CRITICAL,
            "data_exfiltration": ThreatLevel.CRITICAL,
            "sandbox_escape": ThreatLevel.CRITICAL,
            "mcp_poisoning": ThreatLevel.HIGH,
            "repo_poisoning": ThreatLevel.HIGH,
            "adversarial_vars": ThreatLevel.MEDIUM,
            "social_engineering": ThreatLevel.MEDIUM,
            "token_smuggling": ThreatLevel.HIGH,
            "context_overflow": ThreatLevel.MEDIUM,
            "model_extraction": ThreatLevel.CRITICAL,
            "training_poisoning": ThreatLevel.CRITICAL,
            "api_abuse": ThreatLevel.MEDIUM,
            "supply_chain": ThreatLevel.HIGH,
        }
        return levels.get(group_name, ThreatLevel.LOW)
    
    def _calculate_confidence(self, group_name: str, matched_text: str) -> float:
        """Calcular confianza de la detección"""
        base_confidence = {
            "prompt_injection": 0.85,
            "jailbreak": 0.9,
            "data_exfiltration": 0.8,
            "sandbox_escape": 0.95,
            "mcp_poisoning": 0.75,
            "repo_poisoning": 0.8,
            "adversarial_vars": 0.7,
            "social_engineering": 0.65,
            "token_smuggling": 0.8,
            "context_overflow": 0.7,
            "model_extraction": 0.85,
            "training_poisoning": 0.8,
            "api_abuse": 0.7,
            "supply_chain": 0.75,
        }.get(group_name, 0.5)
        
        # Adjust based on match length (longer = more specific)
        if len(matched_text) > 20:
            base_confidence = min(base_confidence + 0.1, 1.0)
        
        return base_confidence
    
    def _get_mitigations(self, threat_types: set) -> List[str]:
        """Obtener mitigaciones recomendadas"""
        mitigations = []
        
        if "prompt_injection" in threat_types:
            mitigations.extend([
                "Sanitize user input before passing to LLM",
                "Use system prompts with clear boundaries",
                "Implement input validation",
            ])
        
        if "jailbreak" in threat_types:
            mitigations.extend([
                "Block request immediately",
                "Log incident for review",
                "Apply content filter",
            ])
        
        if "data_exfiltration" in threat_types:
            mitigations.extend([
                "Block network requests from LLM output",
                "Sanitize URLs in output",
                "Implement output filtering",
            ])
        
        if "sandbox_escape" in threat_types:
            mitigations.extend([
                "Block dangerous commands",
                "Use sandboxed execution",
                "Validate file paths",
            ])
        
        if "mcp_poisoning" in threat_types:
            mitigations.extend([
                "Validate MCP tool definitions",
                "Use allowlist for tools",
                "Audit tool schemas",
            ])
        
        if "repo_poisoning" in threat_types:
            mitigations.extend([
                "Scan AI editor configs",
                "Validate .CLAUDE.md and similar files",
                "Check for hidden payloads",
            ])
        
        if "adversarial_vars" in threat_types:
            mitigations.extend([
                "Review variable names",
                "Validate code before execution",
                "Use static analysis",
            ])
        
        if "social_engineering" in threat_types:
            mitigations.extend([
                "Verify user identity and authorization",
                "Apply consistent security policies",
                "Log social engineering attempts",
            ])
        
        if "token_smuggling" in threat_types:
            mitigations.extend([
                "Decode and sanitize all encoded input",
                "Block encoded payloads",
                "Validate input encoding",
            ])
        
        if "context_overflow" in threat_types:
            mitigations.extend([
                "Limit output length",
                "Implement token budget",
                "Detect recursive patterns",
            ])
        
        if "model_extraction" in threat_types:
            mitigations.extend([
                "Rate limit queries",
                "Detect extraction patterns",
                "Add noise to responses",
            ])
        
        if "training_poisoning" in threat_types:
            mitigations.extend([
                "Validate training data sources",
                "Audit data pipelines",
                "Implement data provenance",
            ])
        
        if "api_abuse" in threat_types:
            mitigations.extend([
                "Enforce rate limits",
                "Implement circuit breakers",
                "Monitor API usage patterns",
            ])
        
        if "supply_chain" in threat_types:
            mitigations.extend([
                "Verify package signatures",
                "Use dependency scanning",
                "Audit build pipelines",
            ])
        
        return mitigations
    
    def get_stats(self) -> Dict:
        """Obtener estadísticas de detección"""
        return {
            "total_scans": self.detection_count,
            "false_positives": self.false_positive_count,
            "pattern_groups": len(self._COMPILED_PATTERNS),
            "total_patterns": sum(len(p) for p in self._COMPILED_PATTERNS.values()),
        }
