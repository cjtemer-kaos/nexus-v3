import { AlertTriangle, ArrowRight, Check, FileMinus, FilePlus, FilePen, FlaskConical } from 'lucide-react'
import { cn } from '@/lib/utils'

type PatchResult = {
  ok?: boolean
  dry_run?: boolean
  files?: string[]
  operations?: Record<string, string>
  moves?: Record<string, string>
  fuzz?: number
  git_checkpoint?: string
  error?: string
}

const OP_META: Record<string, { label: string; color: string; Icon: typeof FilePen }> = {
  add: { label: 'nuevo', color: 'text-[#3DDC84]', Icon: FilePlus },
  delete: { label: 'borrado', color: 'text-[#FF6B6B]', Icon: FileMinus },
  update: { label: 'editado', color: 'text-[#8CC1FF]', Icon: FilePen },
}

export function ApplyPatchResult({ content }: { content: string }) {
  let data: PatchResult | null = null
  try {
    data = JSON.parse(content)
  } catch {
    data = null
  }
  if (!data || typeof data !== 'object') {
    return <pre className="whitespace-pre-wrap break-words">{content}</pre>
  }

  const files = data.files || []
  const operations = data.operations || {}
  const moves = data.moves || {}
  const fuzz = Number(data.fuzz || 0)

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn(
          'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold',
          data.dry_run ? 'bg-[#4C9DFF]/15 text-[#8CC1FF]' : 'bg-[#3DDC84]/15 text-[#3DDC84]',
        )}>
          {data.dry_run ? <FlaskConical className="h-3 w-3" /> : <Check className="h-3 w-3" />}
          {data.dry_run ? 'dry-run (sin escribir)' : 'aplicado'}
        </span>
        <span className="text-[11px] text-[#9BA1AA]">{files.length} archivo{files.length === 1 ? '' : 's'}</span>
        {fuzz > 0 && (
          <span className="inline-flex items-center gap-1 rounded-full bg-[#FFB86B]/15 px-2 py-0.5 text-[10px] font-semibold text-[#FFB86B]">
            <AlertTriangle className="h-3 w-3" /> fuzz {fuzz}
          </span>
        )}
        {data.git_checkpoint && (
          <span className="rounded-full bg-[#22262D] px-2 py-0.5 font-mono text-[10px] text-[#9BA1AA]">
            checkpoint {data.git_checkpoint.slice(0, 8)}
          </span>
        )}
      </div>

      <ul className="space-y-1">
        {files.map((path) => {
          const op = operations[path] || 'update'
          const meta = OP_META[op] || OP_META.update
          const dest = moves[path]
          return (
            <li key={path} className="flex items-start gap-2 text-[11px]">
              <meta.Icon className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', meta.color)} />
              <span className="min-w-0 flex-1 break-all font-mono text-[#C9D2E0]">{path}</span>
              {dest && (
                <span className="flex shrink-0 items-center gap-1 font-mono text-[#FFB86B]">
                  <ArrowRight className="h-3 w-3" /> {dest}
                </span>
              )}
              <span className={cn('shrink-0 text-[10px] uppercase', meta.color)}>{meta.label}</span>
            </li>
          )
        })}
      </ul>

      {data.dry_run && (
        <div className="text-[10px] text-[#8CC1FF]">
          Validación únicamente: volvé a llamar con dry_run=false para aplicar.
        </div>
      )}
      {data.error && <div className="text-[11px] text-[#FF6B6B]">{data.error}</div>}
    </div>
  )
}
