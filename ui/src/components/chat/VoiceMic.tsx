import { useCallback, useEffect, useRef, useState } from 'react'
import { Mic, Square, X, AlertTriangle } from 'lucide-react'
import { PulseOrb } from '@/voice/pulse-orb'
import { useAudioLevel } from '@/voice/use-audio-level'
import { voiceStatusApi, voiceSttApi } from '@/lib/api'
import type { OrbState } from '@/voice/orb-state'

/**
 * Micrófono DECK — contrato 7 estados de voiceorbs (MIT, ver voice/LICENSE):
 * idle · connecting · listening · thinking · speaking · error · disabled
 * 'speaking' queda reservado para TTS (cuando haya modelos piper instalados).
 *
 * Pipeline: grabación (MediaRecorder) → POST /api/voice/stt → texto.
 * Fallback: webkitSpeechRecognition del navegador si el STT local no está.
 */

const ACCENT = '#4C9DFF'
const FILL = '#0B62D6'
const ERR = '#FF6259'

type SpeechRecognitionLike = {
  lang: string
  interimResults: boolean
  continuous: boolean
  start: () => void
  stop: () => void
  abort?: () => void
  onresult: ((e: { resultIndex: number; results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null
  onerror: ((e: { error?: string }) => void) | null
  onend: (() => void) | null
}

const getSR = (): (new () => SpeechRecognitionLike) | null => {
  const w = window as unknown as { SpeechRecognition?: new () => SpeechRecognitionLike; webkitSpeechRecognition?: new () => SpeechRecognitionLike }
  return w.SpeechRecognition || w.webkitSpeechRecognition || null
}

const labelFor = (phase: OrbState, hint: string): string => {
  switch (phase) {
    case 'connecting': return 'Conectando…'
    case 'listening': return 'Escuchando…'
    case 'thinking': return 'Transcribiendo…'
    case 'error': return hint || 'Error de voz'
    case 'disabled': return 'Voz no disponible'
    case 'speaking': return 'Hablando…'
    default: return 'Micrófono listo'
  }
}

export function VoiceMic({ onTranscript }: { onTranscript: (text: string) => void }) {
  const [phase, setPhase] = useState<OrbState>('idle')
  const [hint, setHint] = useState('')
  const [interim, setInterim] = useState('')
  const [sttAvailable, setSttAvailable] = useState<boolean | null>(null)

  const recRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const cancelledRef = useRef(false)
  const srRef = useRef<SpeechRecognitionLike | null>(null)
  const timerRef = useRef<number>(0)

  const { levelRef, error: audioError } = useAudioLevel(phase === 'listening')

  // VAD visual: si el medidor de nivel falla (sin permiso/contexto inseguro),
  // se informa en el overlay en vez de fallar en silencio.

  useEffect(() => {
    let alive = true
    voiceStatusApi()
      .then((s) => { if (alive) setSttAvailable(Boolean(s.stt_available)) })
      .catch(() => { if (alive) setSttAvailable(null) }) // desconocido: intentar y caer a fallback
    return () => { alive = false }
  }, [])

  const cleanup = useCallback(() => {
    window.clearTimeout(timerRef.current)
    try { if (recRef.current?.state === 'recording') recRef.current.stop() } catch { /* noop */ }
    recRef.current = null
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    try { srRef.current?.stop() } catch { /* noop */ }
    srRef.current = null
  }, [])

  useEffect(() => () => { cancelledRef.current = true; cleanup() }, [cleanup])

  const fail = useCallback((msg: string) => {
    cleanup()
    setPhase('error')
    setHint(msg)
    window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => { setPhase('idle'); setHint('') }, 3200)
  }, [cleanup])

  const processRecording = useCallback(async () => {
    const chunks = chunksRef.current
    chunksRef.current = []
    if (cancelledRef.current) return
    if (!chunks.length) { fail('No se capturó audio'); return }
    setPhase('thinking')
    try {
      const blob = new Blob(chunks, { type: chunks[0]?.type || 'audio/webm' })
      const res = await voiceSttApi(blob, navigator.language.slice(0, 2) || 'es')
      if (cancelledRef.current) return // desmontado/cancelado durante el await
      const text = (res.text || '').trim()
      if (text) { onTranscript(text); setPhase('idle'); setHint('') }
      else fail('No se entendió el audio — repite más cerca del micrófono')
    } catch (e) {
      if (cancelledRef.current) return
      const msg = (e as Error).message || String(e)
      // 503 = STT local no disponible (501 en versiones viejas); cae al STT del navegador.
      if ((msg.includes('503') || msg.includes('501') || /no disponible|not available/i.test(msg)) && getSR()) { startSR(); return }
      fail(msg.slice(0, 160))
    } finally {
      streamRef.current?.getTracks().forEach((t) => t.stop())
      streamRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fail, onTranscript])

  const startRecorder = useCallback(async () => {
    setPhase('connecting')
    setHint('')
    cancelledRef.current = false
    let hung = false
    // guardia: getUserMedia puede no resolver nunca (prompt de permiso colgado)
    timerRef.current = window.setTimeout(() => { hung = true; fail('Sin respuesta del micrófono — reintenta') }, 8000)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      window.clearTimeout(timerRef.current)
      if (hung || cancelledRef.current) { stream.getTracks().forEach((t) => t.stop()); return }
      streamRef.current = stream
      const rec = new MediaRecorder(stream)
      chunksRef.current = []
      rec.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data) }
      rec.onstop = () => { void processRecording() }
      recRef.current = rec
      rec.start()
      setPhase('listening')
      // corte de seguridad a los 30s
      timerRef.current = window.setTimeout(() => { if (rec.state === 'recording') rec.stop() }, 30000)
    } catch (e) {
      if (cancelledRef.current || hung) return
      const name = (e as Error).name
      if (name === 'NotAllowedError' || name === 'SecurityError') fail('Permiso de micrófono denegado')
      else if (!navigator.mediaDevices?.getUserMedia && getSR()) startSR()
      else fail('Micrófono no disponible: ' + (name || (e as Error).message))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fail, processRecording])

  function startSR() {
    const SR = getSR()
    if (!SR) { fail('STT del navegador no soportado (usa Chrome/Edge)'); return }
    const sr = new SR()
    sr.lang = navigator.language || 'es-ES'
    sr.interimResults = true
    sr.continuous = false
    cancelledRef.current = false
    let finalText = ''
    sr.onresult = (e) => {
      let interimText = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i]
        const t = r[0]?.transcript ?? ''
        if (r.isFinal) finalText += t
        else interimText += t
      }
      setInterim(interimText || finalText)
    }
    sr.onerror = (e) => { if (!cancelledRef.current) fail('Reconocimiento falló: ' + (e.error || 'error')) }
    sr.onend = () => {
      setInterim('')
      if (cancelledRef.current) return
      const text = finalText.trim()
      if (text) { onTranscript(text); setPhase('idle'); setHint('') }
      else fail('No se escuchó nada')
    }
    srRef.current = sr
    setPhase('connecting')
    try { sr.start(); setPhase('listening') } catch (e) { fail((e as Error).message) }
  }

  const start = () => {
    window.clearTimeout(timerRef.current)
    setHint('')
    if (sttAvailable === false) startSR()
    else void startRecorder()
  }

  const finish = () => {
    window.clearTimeout(timerRef.current)
    try {
      if (recRef.current?.state === 'recording') recRef.current.stop()
      else if (srRef.current) srRef.current.stop()
      else setPhase('idle')
    } catch { setPhase('idle') }
  }

  const cancel = () => {
    cancelledRef.current = true
    cleanup()
    setInterim('')
    setPhase('idle')
    setHint('')
  }

  const active = phase !== 'idle' && phase !== 'disabled'
  const errorColors = phase === 'error'
  const orbProps = {
    state: phase,
    colorFrom: errorColors ? ERR : ACCENT,
    colorTo: errorColors ? '#FF8A80' : FILL,
    levelRef,
  }

  return (
    <>
      {/* botón mic — orbe miniatura (DECK) */}
      <button
        type="button"
        aria-label={labelFor(phase, hint)}
        title={labelFor(phase, hint)}
        disabled={phase === 'disabled' || phase === 'thinking'}
        onClick={() => { if (phase === 'listening') finish(); else if (phase === 'error') start(); else if (phase === 'idle') start(); else if (phase === 'connecting') cancel() }}
        className={`relative flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border transition-colors focus-deck ${
          phase === 'disabled'
            ? 'border-[#2C3037] bg-[#1E2127] opacity-40'
            : active
              ? 'border-[#4C9DFF]/60 bg-[#0B62D6]/20'
              : 'border-[#2C3037] bg-[#22262D] hover:border-[#4C9DFF]/50'
        }`}
      >
        <span className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <PulseOrb {...orbProps} size={30} speed={phase === 'listening' ? 1.4 : 1} label="Estado de voz" />
        </span>
        {phase === 'disabled' && <Mic className="h-3.5 w-3.5 text-[#9BA1AA]" />}
      </button>

      {/* overlay de estado — orbe grande */}
      {active && (
        <div className="absolute bottom-full left-1/2 z-40 mb-3 -translate-x-1/2">
          <div className="flex flex-col items-center gap-2 rounded-2xl border border-[#2C3037] bg-[#22262D] px-6 py-4 shadow-2xl">
            <PulseOrb {...orbProps} size={132} speed={phase === 'listening' ? 1.3 : 1} label={labelFor(phase, hint)} />
            <div className="max-w-[240px] text-center text-xs text-[#9BA1AA]">
              {phase === 'error' ? 'Error de voz' : labelFor(phase, hint)}
              {interim && <div className="mt-1 font-medium text-[#EDEEF0]">“{interim}”</div>}
              {audioError && phase === 'listening' && (
                <div className="mt-1 text-[11px] text-[#FFB020]">
                  {audioError === 'permission-denied'
                    ? 'Medidor de nivel sin permiso de micrófono (la grabación sigue activa)'
                    : 'Medidor de nivel no disponible (la grabación sigue activa)'}
                </div>
              )}
            </div>
            {phase === 'error' && <div className="flex items-center gap-1 text-[11px] text-[#FF6259]"><AlertTriangle className="h-3 w-3" /> {hint}</div>}
            {(phase === 'listening' || phase === 'connecting') && (
              <div className="flex gap-2">
                <button type="button" onClick={() => { if (phase === 'connecting') cancel(); else finish() }} className="flex items-center gap-1.5 rounded-lg bg-[#0B62D6] px-3 py-1.5 text-xs font-medium text-white hover:bg-[#4C9DFF]">
                  <Square className="h-3 w-3" /> Detener
                </button>
                <button type="button" onClick={cancel} className="flex items-center gap-1.5 rounded-lg border border-[#2C3037] px-3 py-1.5 text-xs text-[#9BA1AA] hover:text-[#EDEEF0]">
                  <X className="h-3 w-3" /> Cancelar
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  )
}
