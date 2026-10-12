import { useState } from 'react'
import { useAppStore } from '@/store/app-store'
import { planApi, chatApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { ListChecks, Wand2, Play, Loader2, Plus, X } from 'lucide-react'

export function PlanPanel() {
  const { activeSession } = useAppStore()
  const [task, setTask] = useState('')
  const [steps, setSteps] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [reply, setReply] = useState('')
  const [error, setError] = useState<string | null>(null)

  const generate = async () => {
    const t = task.trim()
    if (!t) return
    setLoading(true); setError(null); setReply('')
    try {
      const r = await planApi(t)
      if (!r.ok) setError(r.error || 'no se pudo generar el plan')
      setSteps(r.steps || [])
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const run = async () => {
    if (!steps.length) return
    setRunning(true); setError(null); setReply('')
    const planText = `Ejecutá este plan paso a paso y completá la tarea:\n${steps.map((s, i) => `${i + 1}. ${s}`).join('\n')}`
    try {
      const r = await chatApi(planText, activeSession || undefined)
      setReply(r.reply || '(sin respuesta)')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-[#2C3037] px-4 py-3">
        <ListChecks className="h-4 w-4 text-[#4C9DFF]" />
        <span className="text-sm font-medium">Plan editable</span>
        <span className="ml-auto text-xs text-[#9BA1AA]">{steps.length} pasos · revisá y editá antes de ejecutar</span>
      </div>

      <div className="flex items-center gap-2 border-b border-[#2C3037] px-4 py-2">
        <input value={task} onChange={(e) => setTask(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') void generate() }}
          placeholder="Describí la tarea… (Enter para generar el plan)"
          className="flex-1 rounded-lg border border-[#2C3037] bg-[#1E2127] px-3 py-1.5 text-sm focus:outline-none focus:border-[#4C9DFF]/50" />
        <Button variant="outline" size="sm" disabled={loading} onClick={() => void generate()}>
          {loading ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Wand2 className="mr-1 h-3.5 w-3.5" />} Generar
        </Button>
        <Button size="sm" disabled={running || !steps.length} onClick={() => void run()}>
          {running ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Play className="mr-1 h-3.5 w-3.5" />} Ejecutar
        </Button>
      </div>

      {error && <div className="border-b border-[#2C3037] bg-[#FF6B6B]/10 px-4 py-2 text-xs text-[#FFA8A8]">{error}</div>}

      <ScrollArea className="flex-1">
        <div className="p-4">
          {steps.length === 0 && !loading && (
            <div className="text-xs text-[#9BA1AA]">Generá un plan para verlo acá. Podés editar cada paso antes de ejecutarlo.</div>
          )}
          {steps.map((s, i) => (
            <div key={i} className="mb-2 flex items-start gap-2">
              <span className="mt-2 w-5 shrink-0 text-right font-mono text-xs text-[#9BA1AA]">{i + 1}.</span>
              <textarea value={s} onChange={(e) => setSteps((prev) => prev.map((x, k) => (k === i ? e.target.value : x)))}
                rows={Math.max(1, Math.ceil(s.length / 80))}
                className="flex-1 resize-none rounded-lg border border-[#2C3037] bg-[#1E2127] px-3 py-1.5 text-sm focus:outline-none focus:border-[#4C9DFF]/50" />
              <button onClick={() => setSteps((prev) => prev.filter((_, k) => k !== i))} className="mt-2 text-[#9BA1AA] hover:text-[#FFA8A8]" title="Quitar paso"><X className="h-3.5 w-3.5" /></button>
            </div>
          ))}
          {steps.length > 0 && (
            <button onClick={() => setSteps((prev) => [...prev, ''])} className="mt-1 flex items-center gap-1 text-xs text-[#8CC1FF] hover:underline">
              <Plus className="h-3.5 w-3.5" /> Agregar paso
            </button>
          )}

          {reply && (
            <div className="mt-4 rounded-lg border border-[#2C3037] bg-[#1E2127] p-3">
              <div className="mb-1 text-xs font-medium text-[#3DDC84]">Resultado</div>
              <div className="whitespace-pre-wrap text-xs text-[#EDEEF0]">{reply}</div>
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
  )
}
