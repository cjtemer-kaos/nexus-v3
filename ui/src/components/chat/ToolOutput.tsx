import { cn } from '@/lib/utils'

// Color por línea, igual que la vista Cambios: verde agrega, rojo saca, azul los hunk.
const claseDeLinea = (line: string) => {
  if (line.startsWith('+++') || line.startsWith('---')) return 'text-[#9BA1AA]'
  if (line.startsWith('+')) return 'bg-[#3DDC84]/10 text-[#7BE8A8]'
  if (line.startsWith('-')) return 'bg-[#FF6B6B]/10 text-[#FFA8A8]'
  if (line.startsWith('@@')) return 'text-[#4C9DFF]'
  return 'text-[#C9D2E0]'
}

function Lineas({ texto }: { texto: string }) {
  return (
    <div className="font-mono">
      {texto.split('\n').map((line, i) => (
        <div key={i} className={cn('whitespace-pre-wrap break-words px-1', claseDeLinea(line))}>
          {line || ' '}
        </div>
      ))}
    </div>
  )
}

// Salida de tool: los bloques ```diff se pintan por línea (como opencode) en vez
// de quedar como texto plano con los +/- mezclados.
export function ToolOutput({ content }: { content: string }) {
  const texto = content || ''
  const esDiffCrudo = /^(diff --git|--- a\/|\+\+\+ b\/)/m.test(texto.slice(0, 400)) && texto.includes('\n@@')
  if (esDiffCrudo) return <Lineas texto={texto} />

  const partes = texto.split(/```diff\n?/i)
  if (partes.length === 1) return <>{texto}</>

  return (
    <>
      {partes.map((parte, i) => {
        if (i === 0) return parte ? <span key={i}>{parte}</span> : null
        const fin = parte.indexOf('```')
        const cuerpo = fin >= 0 ? parte.slice(0, fin) : parte
        const resto = fin >= 0 ? parte.slice(fin + 3) : ''
        return (
          <span key={i}>
            <div className="my-1 overflow-hidden rounded border border-[#2C3037] bg-[#15171B] py-1 text-[11px] leading-relaxed">
              <Lineas texto={cuerpo.replace(/\n$/, '')} />
            </div>
            {resto && <span className="whitespace-pre-wrap">{resto}</span>}
          </span>
        )
      })}
    </>
  )
}
