"""
Email Integration - SuperNEXUS v2
IMAP/SMTP con AI auto-reply, auto-summarize, auto-tag.
"""

import asyncio
import email
import email.header
import json
import logging
import os
import time
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("NEXUS_DATA", Path.home() / ".nexus")) / "email"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ACCOUNTS_FILE = DATA_DIR / "accounts.json"


class EmailAccount:
    def __init__(self, data: Dict):
        self.id: str = data.get("id", "")
        self.name: str = data.get("name", "")
        self.email: str = data.get("email", "")
        self.imap_host: str = data.get("imap_host", "")
        self.imap_port: int = data.get("imap_port", 993)
        self.smtp_host: str = data.get("smtp_host", "")
        self.smtp_port: int = data.get("smtp_port", 587)
        self.username: str = data.get("username", "")
        self.password: str = data.get("password", "")
        self.use_ssl: bool = data.get("use_ssl", True)
        self.enabled: bool = data.get("enabled", True)

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "name": self.name, "email": self.email,
            "imap_host": self.imap_host, "imap_port": self.imap_port,
            "smtp_host": self.smtp_host, "smtp_port": self.smtp_port,
            "username": self.username, "password": "***" if self.password else "",
            "use_ssl": self.use_ssl, "enabled": self.enabled,
        }

    def to_safe_dict(self) -> Dict:
        d = self.to_dict()
        d["password"] = ""
        return d


class EmailManager:
    """Gestor de email IMAP/SMTP con AI auto-reply."""

    def __init__(self, llm_caller=None):
        self.accounts: Dict[str, EmailAccount] = {}
        self._llm_caller = llm_caller
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._load()

    def _load(self):
        try:
            if ACCOUNTS_FILE.exists():
                data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
                for a in data.get("accounts", []):
                    acc = EmailAccount(a)
                    self.accounts[acc.id] = acc
        except Exception as e:
            logger.error(f"Error cargando cuentas: {e}")

    def _save(self):
        try:
            ACCOUNTS_FILE.write_text(json.dumps({
                "accounts": [a.to_dict() for a in self.accounts.values()]
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error guardando cuentas: {e}")

    def add_account(self, data: Dict) -> EmailAccount:
        import uuid
        acc = EmailAccount({"id": str(uuid.uuid4())[:8], **data})
        self.accounts[acc.id] = acc
        self._save()
        return acc

    def remove_account(self, account_id: str) -> bool:
        if account_id in self.accounts:
            del self.accounts[account_id]
            self._save()
            return True
        return False

    def list_accounts(self) -> List[Dict]:
        return [a.to_safe_dict() for a in self.accounts.values()]

    def _get_imap_connection(self, account: EmailAccount):
        """Obtener conexion IMAP con cache"""
        cache_key = f"imap-{account.id}"
        if cache_key in self._cache:
            ts, conn = self._cache[cache_key]
            if time.time() - ts < 300:
                return conn
        try:
            import imaplib
            if account.use_ssl:
                conn = imaplib.IMAP4_SSL(account.imap_host, account.imap_port)
            else:
                conn = imaplib.IMAP4(account.imap_host, account.imap_port)
            conn.login(account.username, account.password)
            self._cache[cache_key] = (time.time(), conn)
            return conn
        except Exception as e:
            logger.error(f"IMAP error: {e}")
            return None

    async def list_emails(self, account_id: str, folder: str = "INBOX", limit: int = 20) -> List[Dict]:
        """Listar emails via IMAP"""
        account = self.accounts.get(account_id)
        if not account:
            return []

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._list_emails_sync, account, folder, limit)

    def _list_emails_sync(self, account: EmailAccount, folder: str, limit: int) -> List[Dict]:
        conn = self._get_imap_connection(account)
        if not conn:
            return []
        try:
            conn.select(folder)
            _, msg_nums = conn.search(None, "ALL")
            nums = msg_nums[0].split()[-limit:]
            nums.reverse()

            emails = []
            for num in nums:
                _, msg_data = conn.fetch(num, "(RFC822.HEADER)")
                raw = msg_data[0][1]
                if isinstance(raw, bytes):
                    msg = email.message_from_bytes(raw)
                else:
                    continue

                subject = ""
                for part, enc in email.header.decode_header(msg.get("Subject", "")):
                    if isinstance(part, bytes):
                        subject += part.decode(enc or "utf-8", errors="replace")
                    else:
                        subject += part

                from_addr = msg.get("From", "")
                date_str = msg.get("Date", "")
                uid = num.decode() if isinstance(num, bytes) else str(num)

                emails.append({
                    "uid": uid,
                    "from": from_addr,
                    "subject": subject,
                    "date": date_str,
                    "has_attachments": any(m.get_content_type().startswith("multipart") for m in msg.walk()),
                })
            return emails
        except Exception as e:
            logger.error(f"IMAP list error: {e}")
            return []

    async def read_email(self, account_id: str, uid: str, folder: str = "INBOX") -> Dict:
        """Leer email completo"""
        account = self.accounts.get(account_id)
        if not account:
            return {"error": "Cuenta no encontrada"}

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._read_email_sync, account, uid, folder)

    def _read_email_sync(self, account: EmailAccount, uid: str, folder: str) -> Dict:
        conn = self._get_imap_connection(account)
        if not conn:
            return {"error": "Error de conexion"}
        try:
            conn.select(folder)
            _, msg_data = conn.fetch(uid.encode() if isinstance(uid, str) else uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw) if isinstance(raw, bytes) else {}

            subject = ""
            for part, enc in email.header.decode_header(msg.get("Subject", "")):
                if isinstance(part, bytes):
                    subject += part.decode(enc or "utf-8", errors="replace")
                else:
                    subject += part

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                            break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

            return {
                "uid": uid,
                "from": msg.get("From", ""),
                "to": msg.get("To", ""),
                "subject": subject,
                "date": msg.get("Date", ""),
                "body": body[:10000],
            }
        except Exception as e:
            return {"error": str(e)}

    async def send_email(self, account_id: str, to: str, subject: str, body: str, cc: str = "") -> Dict:
        """Enviar email via SMTP"""
        account = self.accounts.get(account_id)
        if not account:
            return {"error": "Cuenta no encontrada"}

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_email_sync, account, to, subject, body, cc)

    def _send_email_sync(self, account: EmailAccount, to: str, subject: str, body: str, cc: str) -> Dict:
        try:
            import smtplib
            msg = MIMEText(body, "plain", "utf-8")
            msg["From"] = account.email
            msg["To"] = to
            msg["Subject"] = subject
            if cc:
                msg["Cc"] = cc

            if account.use_ssl:
                server = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port)
            else:
                server = smtplib.SMTP(account.smtp_host, account.smtp_port)
                server.starttls()

            server.login(account.username, account.password)
            recipients = [to] + ([cc] if cc else [])
            server.sendmail(account.email, recipients, msg.as_string())
            server.quit()
            return {"success": True, "message": "Email enviado"}
        except Exception as e:
            return {"error": str(e)}

    async def auto_summarize(self, account_id: str, folder: str = "INBOX", limit: int = 5) -> str:
        """Auto-resumir emails recientes via LLM"""
        emails = await self.list_emails(account_id, folder, limit)
        if not emails:
            return "No hay emails recientes."

        summaries = []
        for em in emails:
            full = await self.read_email(account_id, em["uid"], folder)
            if "body" in full:
                summaries.append(f"De: {full.get('from', '')}\nAsunto: {full.get('subject', '')}\n{full['body'][:500]}")

        if not summaries:
            return "No se pudieron leer los emails."

        combined = "\n\n---\n\n".join(summaries)
        if self._llm_caller:
            return await self._llm_caller(
                f"Resume estos {len(summaries)} emails de forma concisa. "
                f"Destaca lo urgente o importante:\n\n{combined[:4000]}",
                "analyst"
            )
        return combined[:2000]

    def get_status(self) -> Dict:
        return {
            "accounts": len(self.accounts),
            "enabled": sum(1 for a in self.accounts.values() if a.enabled),
        }
