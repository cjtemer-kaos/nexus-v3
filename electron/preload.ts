/**
 * Preload — contextBridge only. No Node exposure.
 * Modern Electron: contextIsolation true, sandbox true, nodeIntegration false.
 */
import { contextBridge, ipcRenderer } from "electron";

export type NexusAPI = {
  getBackendUrl: () => Promise<string>;
  getBackendPort: () => Promise<number>;
  onBackendStatus: (cb: (status: { ok: boolean; url: string }) => void) => () => void;
  restartBackend: () => Promise<{ ok: boolean; url?: string; error?: string }>;
  platform: NodeJS.Platform;
  versions: { electron: string; node: string; chrome: string };
};

const api: NexusAPI = {
  getBackendUrl: () => ipcRenderer.invoke("nexus:getBackendUrl"),
  getBackendPort: () => ipcRenderer.invoke("nexus:getBackendPort"),
  onBackendStatus: (cb) => {
    const handler = (_: unknown, status: { ok: boolean; url: string }) => cb(status);
    ipcRenderer.on("nexus:backendStatus", handler);
    return () => ipcRenderer.removeListener("nexus:backendStatus", handler);
  },
  restartBackend: () => ipcRenderer.invoke("nexus:restartBackend"),
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    node: process.versions.node,
    chrome: process.versions.chrome,
  },
};

contextBridge.exposeInMainWorld("nexus", api);

// Types for renderer
declare global {
  interface Window {
    nexus: NexusAPI;
  }
}
