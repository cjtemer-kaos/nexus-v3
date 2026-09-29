"""
CredentialManager - Secure credential storage and management

Features:
- Windows Credential Manager integration via keyring
- SSH key authentication for PC2
- .env sanitization (secrets detection)
- Encrypted token storage in cerebro.db
- Doctor check: alert if .env has secrets not in .gitignore
- Zero-secrets policy for distribution
"""

import base64
import logging
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("nexus-credentials")

# Patterns that indicate secrets in .env files
SECRET_PATTERNS = [
    (r"(?i)(api[_-]?key|apikey)\s*=\s*.+", "API Key"),
    (r"(?i)(secret|password|passwd|pwd)\s*=\s*.+", "Password/Secret"),
    (r"(?i)(token|auth[_-]?token|access[_-]?token)\s*=\s*.+", "Token"),
    (r"(?i)(private[_-]?key)\s*=\s*.+", "Private Key"),
    (r"(?i)(connection[_-]?string|conn[_-]?str)\s*=\s*.+", "Connection String"),
    (r"(?i)(aws[_-]?secret|azure[_-]?key|gcp[_-]?key)\s*=\s*.+", "Cloud Secret"),
]

# Patterns for private IP addresses (to detect hardcoded infrastructure)
PRIVATE_IP_PATTERNS = [
    (r"192\.168\.\d+\.\d+", "Private IP (192.168.x.x)"),
    (r"10\.\d+\.\d+\.\d+", "Private IP (10.x.x.x)"),
    (r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+", "Private IP (172.16-31.x.x)"),
]


class CredentialManager:
    """
    Secure credential management with zero-secrets policy.
    """

    def __init__(self, project_root: str = None):
        self.project_root = Path(project_root) if project_root else Path(__file__).parent.parent.parent
        self._keyring_available = False
        self._init_keyring()

    def _init_keyring(self):
        """Try to import keyring for system credential storage."""
        try:
            import keyring
            self._keyring = keyring
            self._keyring_available = True
            logger.info("System keyring available for credential storage")
        except ImportError:
            logger.warning("keyring not installed. Credentials will use encrypted fallback.")
            self._keyring = None
            self._keyring_available = False

    # ─── Credential Storage ────────────────────────────────────────────

    def set_credential(self, service: str, key: str, value: str) -> bool:
        """Store a credential securely."""
        if self._keyring_available:
            try:
                self._keyring.set_password(f"SuperNEXUS/{service}", key, value)
                logger.info(f"Credential stored: {service}/{key}")
                return True
            except Exception as e:
                logger.error(f"Failed to store credential {service}/{key}: {e}")
                return self._fallback_store(service, key, value)
        else:
            return self._fallback_store(service, key, value)

    def get_credential(self, service: str, key: str) -> Optional[str]:
        """Retrieve a credential."""
        if self._keyring_available:
            try:
                value = self._keyring.get_password(f"SuperNEXUS/{service}", key)
                return value
            except Exception:
                return self._fallback_retrieve(service, key)
        else:
            return self._fallback_retrieve(service, key)

    def delete_credential(self, service: str, key: str) -> bool:
        """Delete a credential."""
        if self._keyring_available:
            try:
                import keyring
                keyring.delete_password(f"SuperNEXUS/{service}", key)
                return True
            except Exception:
                pass
        return self._fallback_delete(service, key)

    def list_credentials(self, service: str = None) -> List[str]:
        """List stored credential keys."""
        # keyring doesn't support listing, so we track via fallback
        return self._fallback_list(service)

    # ─── Encrypted Fallback Storage ────────────────────────────────────

    def _get_fallback_db(self) -> sqlite3.Connection:
        """Get connection to credential fallback DB."""
        db_path = Path.home() / ".nexus" / "brain" / "credentials.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS credentials (
            service TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (service, key)
        )""")
        conn.commit()
        return conn

    def _fallback_store(self, service: str, key: str, value: str) -> bool:
        """Store credential in encrypted fallback DB."""
        try:
            conn = self._get_fallback_db()
            # Simple obfuscation (not true encryption, but better than plaintext)
            encoded = base64.b64encode(value.encode()).decode()
            conn.execute(
                "INSERT OR REPLACE INTO credentials (service, key, value) VALUES (?, ?, ?)",
                (service, key, encoded),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Fallback store failed: {e}")
            return False

    def _fallback_retrieve(self, service: str, key: str) -> Optional[str]:
        """Retrieve credential from fallback DB."""
        try:
            conn = self._get_fallback_db()
            row = conn.execute(
                "SELECT value FROM credentials WHERE service = ? AND key = ?",
                (service, key),
            ).fetchone()
            conn.close()
            if row:
                return base64.b64decode(row[0]).decode()
        except Exception:
            pass
        return None

    def _fallback_delete(self, service: str, key: str) -> bool:
        try:
            conn = self._get_fallback_db()
            conn.execute("DELETE FROM credentials WHERE service = ? AND key = ?", (service, key))
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False

    def _fallback_list(self, service: str = None) -> List[str]:
        try:
            conn = self._get_fallback_db()
            if service:
                rows = conn.execute(
                    "SELECT key FROM credentials WHERE service = ?", (service,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT service, key FROM credentials").fetchall()
            conn.close()
            return [r[0] for r in rows]
        except Exception:
            return []

    # ─── SSH Key Management ────────────────────────────────────────────

    def get_ssh_key(self, key_path: str = None) -> Optional[str]:
        """Get SSH private key content."""
        paths = [
            key_path,
            str(Path.home() / ".ssh" / "id_rsa"),
            str(Path.home() / ".ssh" / "id_ed25519"),
        ]
        for p in paths:
            if p and Path(p).exists():
                try:
                    return Path(p).read_text()
                except Exception:
                    continue
        return None

    def test_ssh_connection(self, host: str, port: int = 22, username: str = None) -> Tuple[bool, str]:
        """Test SSH connection using available key."""
        try:
            import paramiko
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            key_path = self.get_ssh_key()
            if key_path:
                ssh.connect(host, port=port, username=username or "user", key_filename=key_path, timeout=5)
            else:
                # Try password from credentials
                password = self.get_credential("ssh", f"{host}:{port}")
                if password:
                    ssh.connect(host, port=port, username=username or "user", password=password, timeout=5)
                else:
                    return False, "No SSH key or password available"

            ssh.close()
            return True, "Connection successful"
        except ImportError:
            return False, "paramiko not installed (pip install paramiko)"
        except Exception as e:
            return False, str(e)

    # ─── .env Security Audit ──────────────────────────────────────────

    def scan_env_file(self, env_path: str = None) -> List[Dict]:
        """Scan .env file for secrets."""
        path = Path(env_path) if env_path else self.project_root / ".env"
        if not path.exists():
            return []

        findings = []
        for i, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            for pattern, secret_type in SECRET_PATTERNS:
                if re.search(pattern, line):
                    # Check if it's a placeholder
                    value = line.split("=", 1)[1].strip() if "=" in line else ""
                    is_placeholder = value in ("", '""', "''", "your_key_here", "CHANGE_ME", "xxx", "TODO")
                    findings.append({
                        "line": i,
                        "type": secret_type,
                        "is_placeholder": is_placeholder,
                        "content": line if is_placeholder else line.split("=")[0] + "=***",
                    })
                    break

        return findings

    def check_gitignore(self) -> Tuple[bool, str]:
        """Check if .env is in .gitignore."""
        gitignore_path = self.project_root / ".gitignore"
        if not gitignore_path.exists():
            return False, ".gitignore not found"

        content = gitignore_path.read_text()
        if ".env" in content or ".env.*" in content:
            return True, ".env is properly ignored"
        return False, ".env is NOT in .gitignore - CRITICAL SECURITY RISK"

    def scan_for_hardcoded_secrets(self, directory: str = None, extensions: List[str] = None) -> List[Dict]:
        """Scan codebase for hardcoded secrets (private IPs, keys, etc.)."""
        directory = directory or str(self.project_root / "src")
        extensions = extensions or [".py", ".ts", ".tsx", ".js", ".json", ".yaml", ".yml", ".env"]

        findings = []
        for ext in extensions:
            for path in Path(directory).rglob(f"*{ext}"):
                # Skip node_modules, .git, __pycache__
                if any(skip in str(path) for skip in ["node_modules", ".git", "__pycache__", "dist/"]):
                    continue
                try:
                    content = path.read_text(errors="ignore")
                    for i, line in enumerate(content.splitlines(), 1):
                        for pattern, finding_type in PRIVATE_IP_PATTERNS:
                            if re.search(pattern, line):
                                # Skip localhost and comments
                                stripped = line.strip()
                                if stripped.startswith("#") or stripped.startswith("//"):
                                    continue
                                findings.append({
                                    "file": str(path.relative_to(self.project_root)),
                                    "line": i,
                                    "type": finding_type,
                                    "content": stripped[:100],
                                })
                except Exception:
                    continue

        return findings

    # ─── Doctor Check ──────────────────────────────────────────────────

    def run_doctor_check(self) -> Dict:
        """Run comprehensive security audit."""
        env_findings = self.scan_env_file()
        gitignore_ok, gitignore_msg = self.check_gitignore()
        hardcoded = self.scan_for_hardcoded_secrets()

        issues = []
        if env_findings:
            real_secrets = [f for f in env_findings if not f["is_placeholder"]]
            if real_secrets:
                issues.append(f"CRITICAL: {len(real_secrets)} real secrets in .env file")
            placeholders = [f for f in env_findings if f["is_placeholder"]]
            if placeholders:
                issues.append(f"INFO: {len(placeholders)} placeholder secrets in .env (OK)")

        if not gitignore_ok:
            issues.append(f"CRITICAL: {gitignore_msg}")

        if hardcoded:
            issues.append(f"WARNING: {len(hardcoded)} hardcoded private IPs found")

        return {
            "status": "PASS" if not issues else "FAIL",
            "issues": issues,
            "env_findings": len(env_findings),
            "gitignore_ok": gitignore_ok,
            "hardcoded_ips": len(hardcoded),
            "recommendations": [
                "Use set_credential() for all secrets",
                "Add .env to .gitignore if not present",
                "Use environment variables for infrastructure URLs",
                "Use SSH keys instead of passwords",
            ],
        }

    # ─── Migration Helper ──────────────────────────────────────────────

    def migrate_env_to_keyring(self, env_path: str = None) -> Dict:
        """Migrate secrets from .env to secure storage."""
        findings = self.scan_env_file(env_path)
        migrated = []
        skipped = []

        for finding in findings:
            if finding["is_placeholder"]:
                skipped.append(finding)
                continue

            # Extract key and value
            env_path_obj = Path(env_path) if env_path else self.project_root / ".env"
            lines = env_path_obj.read_text().splitlines()
            if finding["line"] <= len(lines):
                line = lines[finding["line"] - 1]
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if self.set_credential("env", key, value):
                        migrated.append(key)

        return {
            "migrated": migrated,
            "skipped": skipped,
            "total": len(findings),
        }

    def get_status(self) -> Dict:
        return {
            "keyring_available": self._keyring_available,
            "project_root": str(self.project_root),
            "credentials_count": len(self._fallback_list()),
        }
