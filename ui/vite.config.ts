import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5174,
    proxy: {
      // Portable: el destino del backend se define por entorno (sin hardcode).
      '/api': process.env.NEXUS_API_URL || 'http://127.0.0.1:8765',
    },
  },
  build: {
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom'],
          graph: ['react-force-graph-2d', 'd3-force'],
          motion: ['framer-motion'],
        },
      },
    },
  },
})
