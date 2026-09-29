"""NexusBridge - Bidirectional SSH bridge via Tailscale."""
import paramiko
import json
import os
import sys

PC1_TS_IP = "100.116.98.98"
VPS_TS_IP = "100.102.223.47"
VPS_USER = "root"
PC1_USER = "cjtr"
PC1_KEY = r"C:\Users\cjtr\.ssh\id_tailscale"


class NexusBridge:
    """Secure SSH bridge between PC1 and VPS via Tailscale."""
    
    def __init__(self):
        self.vps_host = VPS_TS_IP
        self.vps_user = VPS_USER
        self.pc1_host = PC1_TS_IP
        self.pc1_user = PC1_USER
        self.pc1_key = PC1_KEY
    
    def connect_vps(self):
        """Connect to VPS via Tailscale."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(self.vps_host, username=self.vps_user, key_filename=self.pc1_key, timeout=15)
        return ssh
    
    def execute_on_vps(self, command):
        """Execute command on VPS and return output."""
        ssh = self.connect_vps()
        stdin, stdout, stderr = ssh.exec_command(command)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        ssh.close()
        return out, err
    
    def execute_on_pc1(self, command):
        """Execute command on PC1 from VPS (via hop)."""
        ssh = self.connect_vps()
        stdin, stdout, stderr = ssh.exec_command(
            f"ssh -i ~/.ssh/id_tailscale -o StrictHostKeyChecking=no {self.pc1_user}@{self.pc1_host} '{command}'"
        )
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        ssh.close()
        return out, err
    
    def sync_brain_to_vps(self, local_brain, remote_brain):
        """Sync brain DB from PC1 to VPS."""
        ssh = self.connect_vps()
        sftp = ssh.open_sftp()
        sftp.put(local_brain, remote_brain)
        sftp.close()
        ssh.close()
    
    def sync_brain_from_vps(self, remote_brain, local_brain):
        """Sync brain DB from VPS to PC1."""
        ssh = self.connect_vps()
        sftp = ssh.open_sftp()
        sftp.get(remote_brain, local_brain)
        sftp.close()
        ssh.close()
    
    def get_vps_status(self):
        """Get VPS system status."""
        out, err = self.execute_on_vps(
            "echo '=== VPS STATUS ===' && "
            "hostname && "
            "uptime && "
            "free -h | head -3 && "
            "df -h / | tail -1 && "
            "systemctl is-active minecraft 2>/dev/null && "
            "systemctl is-active nexus 2>/dev/null"
        )
        return out
    
    def get_pc1_status(self):
        """Get PC1 system status via VPS hop."""
        out, err = self.execute_on_pc1("hostname && uptime")
        return f"=== PC1 STATUS ===\n{out}"


def main():
    bridge = NexusBridge()
    
    if len(sys.argv) < 2:
        print("NexusBridge - Tailscale SSH Bridge")
        print("Usage:")
        print("  python nexus_bridge.py vps-status      - Get VPS status")
        print("  python nexus_bridge.py pc1-status      - Get PC1 status")
        print("  python nexus_bridge.py sync-to-vps     - Sync brain PC1 -> VPS")
        print("  python nexus_bridge.py sync-from-vps   - Sync brain VPS -> PC1")
        print("  python nexus_bridge.py exec-vps <cmd>  - Execute on VPS")
        print("  python nexus_bridge.py exec-pc1 <cmd>  - Execute on PC1 via VPS")
        return
    
    cmd = sys.argv[1]
    
    if cmd == "vps-status":
        print(bridge.get_vps_status())
    elif cmd == "pc1-status":
        print(bridge.get_pc1_status())
    elif cmd == "sync-to-vps":
        bridge.sync_brain_to_vps(
            r"D:\ias\proyectos\supernexus-v2\brain\cerebro.db",
            "/opt/nexus/nexus-full/latamrust-nexus/brain/cerebro.db"
        )
        print("Brain synced PC1 -> VPS")
    elif cmd == "sync-from-vps":
        bridge.sync_brain_from_vps(
            "/opt/nexus/nexus-full/latamrust-nexus/brain/cerebro.db",
            r"D:\ias\proyectos\supernexus-v2\brain\cerebro.db"
        )
        print("Brain synced VPS -> PC1")
    elif cmd == "exec-vps" and len(sys.argv) > 2:
        out, err = bridge.execute_on_vps(" ".join(sys.argv[2:]))
        print(out)
        if err:
            print(f"ERR: {err}")
    elif cmd == "exec-pc1" and len(sys.argv) > 2:
        out, err = bridge.execute_on_pc1(" ".join(sys.argv[2:]))
        print(out)
        if err:
            print(f"ERR: {err}")
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
