/**
 * Preload — contextBridge only. No Node exposure.
 * Compiled from preload.ts (keep in sync).
 */
const { contextBridge, ipcRenderer } = require("electron");

const api = {
  getBackendUrl: () => ipcRenderer.invoke("nexus:getBackendUrl"),
  getBackendPort: () => ipcRenderer.invoke("nexus:getBackendPort"),
  onBackendStatus: (cb) => {
    const handler = (_, status) => cb(status);
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
