"""NexusBridge - Bidirectional SSH bridge (opt-in, portable).

Configurado SOLO por env vars — cero hardcodes de esta PC. En una PC nueva
sin configurar, los comandos fallan con mensaje accionable.

Env vars:
  NEXUS_BRIDGE_VPS_HOST      (requerido) host SSH del VPS (p.ej. IP Tailscale)
  NEXUS_BRIDGE_VPS_USER      usuario remoto (default: root)
  NEXUS_BRIDGE_LOCAL_HOST    host de ESTA PC visto desde el VPS (para exec-pc1)
  NEXUS_BRIDGE_LOCAL_USER    usuario local (default: usuario actual)
  NEXUS_BRIDGE_SSH_KEY       path de la key (default: ~/.ssh/id_tailscale)
  NEXUS_BRIDGE_REMOTE_BRAIN  path del cerebro en el VPS (para sync)
  NEXUS_BRAIN                cerebro local (default: <proyecto>/brain)
"""
import getpass
import os
import sys
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _default_local_brain() -> str:
    # Cerebro PROPIO de nexus-v3 (NEXUS_V3_BRAIN); NO usar NEXUS_BRAIN, que
    # pertenece a supernexus-v2 (proyectos separados, no deben cruzarse).
    nb = _env("NEXUS_V3_BRAIN")
    if nb:
        return str(Path(nb).expanduser() / "cerebro.db")
    return str(Path(__file__).resolve().parents[2] / "brain" / "cerebro.db")


VPS_TS_IP = _env("NEXUS_BRIDGE_VPS_HOST")
VPS_USER = _env("NEXUS_BRIDGE_VPS_USER", "root")
PC1_TS_IP = _env("NEXUS_BRIDGE_LOCAL_HOST")
PC1_USER = _env("NEXUS_BRIDGE_LOCAL_USER", getpass.getuser())
PC1_KEY = _env("NEXUS_BRIDGE_SSH_KEY") or str(Path.home() / ".ssh" / "id_tailscale")
REMOTE_BRAIN = _env("NEXUS_BRIDGE_REMOTE_BRAIN")


class NexusBridge:
    """Secure SSH bridge to a remote host (opt-in via env vars)."""

    def __init__(self):
        self.vps_host = VPS_TS_IP
        self.vps_user = VPS_USER
        self.pc1_host = PC1_TS_IP
        self.pc1_user = PC1_USER
        self.pc1_key = PC1_KEY
        self.remote_brain = REMOTE_BRAIN

    def _require_vps(self):
        if not self.vps_host:
            raise RuntimeError(
                "NEXUS_BRIDGE_VPS_HOST no configurado. Setea las env vars "
                "NEXUS_BRIDGE_VPS_HOST / NEXUS_BRIDGE_SSH_KEY (ver docstring "
                "de src/bridges/nexus_bridge.py)."
            )

    def connect_vps(self):
        """Connect to remote host via SSH."""
        self._require_vps()
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(self.vps_host, username=self.vps_user, key_filename=self.pc1_key, timeout=15)
        return ssh

    def execute_on_vps(self, command):
        """Execute command on remote host and return output."""
        ssh = self.connect_vps()
        stdin, stdout, stderr = ssh.exec_command(command)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        ssh.close()
        return out, err

    def execute_on_pc1(self, command):
        """Execute command on THIS machine from remote (via hop)."""
        if not (self.pc1_host and self.pc1_user):
            raise RuntimeError(
                "NEXUS_BRIDGE_LOCAL_HOST/LOCAL_USER no configurados "
                "(necesarios para exec-pc1 / sync via hop)."
            )
        ssh = self.connect_vps()
        stdin, stdout, stderr = ssh.exec_command(
            f"ssh -i {self.pc1_key} -o StrictHostKeyChecking=no "
            f"{self.pc1_user}@{self.pc1_host} '{command}'"
        )
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        ssh.close()
        return out, err

    def sync_brain_to_vps(self, local_brain, remote_brain):
        """Sync brain DB from local to remote."""
        ssh = self.connect_vps()
        sftp = ssh.open_sftp()
        sftp.put(local_brain, remote_brain)
        sftp.close()
        ssh.close()

    def sync_brain_from_vps(self, remote_brain, local_brain):
        """Sync brain DB from remote to local."""
        ssh = self.connect_vps()
        sftp = ssh.open_sftp()
        sftp.get(remote_brain, local_brain)
        sftp.close()
        ssh.close()

    def get_vps_status(self):
        """Get remote system status."""
        out, err = self.execute_on_vps(
            "echo '=== REMOTE STATUS ===' && "
            "hostname && "
            "uptime && "
            "free -h | head -3 && "
            "df -h / | tail -1"
        )
        return out

    def get_pc1_status(self):
        """Get THIS machine status via remote hop."""
        out, err = self.execute_on_pc1("hostname && uptime")
        return f"=== LOCAL STATUS ===\n{out}"


def main():
    bridge = NexusBridge()

    if len(sys.argv) < 2:
        print("NexusBridge - SSH Bridge (env-driven, ver docstring del modulo)")
        print("Usage:")
        print("  python nexus_bridge.py remote-status   - Get remote status")
        print("  python nexus_bridge.py local-status    - Get local status via hop")
        print("  python nexus_bridge.py sync-to-remote  - Sync brain local -> remoto")
        print("  python nexus_bridge.py sync-from-remote- Sync brain remoto -> local")
        print("  python nexus_bridge.py exec-remote <cmd>   - Execute on remote")
        print("  python nexus_bridge.py exec-local <cmd>    - Execute on local via hop")
        return

    cmd = sys.argv[1]
    local_brain = _default_local_brain()

    if cmd == "remote-status":
        print(bridge.get_vps_status())
    elif cmd == "local-status":
        print(bridge.get_pc1_status())
    elif cmd == "sync-to-remote":
        if not bridge.remote_brain:
            raise RuntimeError("NEXUS_BRIDGE_REMOTE_BRAIN no configurado.")
        bridge.sync_brain_to_vps(local_brain, bridge.remote_brain)
        print("Brain synced local -> remoto")
    elif cmd == "sync-from-remote":
        if not bridge.remote_brain:
            raise RuntimeError("NEXUS_BRIDGE_REMOTE_BRAIN no configurado.")
        bridge.sync_brain_from_vps(bridge.remote_brain, local_brain)
        print("Brain synced remoto -> local")
    elif cmd == "exec-remote" and len(sys.argv) > 2:
        out, err = bridge.execute_on_vps(" ".join(sys.argv[2:]))
        print(out)
        if err:
            print(f"ERR: {err}")
    elif cmd == "exec-local" and len(sys.argv) > 2:
        out, err = bridge.execute_on_pc1(" ".join(sys.argv[2:]))
        print(out)
        if err:
            print(f"ERR: {err}")
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
