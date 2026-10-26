import { useState } from 'react'
import { Check, Copy } from 'lucide-react'

// Render de markdown mínimo y seguro (sin dangerouslySetInnerHTML): negritas, cursivas,
// código inline, bloques de código con copiar, listas, títulos y links.
function inline(text: string, keyBase: string) {
  const out: any[] = []
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)\s]+\))/g
  let last = 0
  let m: RegExpExecArray | null
  let i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const tok = m[0]
    const key = `${keyBase}-${i++}`
    if (tok.startsWith('`')) {
      out.push(<code key={key} className="rounded bg-[#15171B] px-1 py-0.5 font-mono text-[0.9em] text-[#FFB86B]">{tok.slice(1, -1)}</code>)
    } else if (tok.startsWith('**')) {
      out.push(<strong key={key} className="font-semibold text-[#EDEEF0]">{tok.slice(2, -2)}</strong>)
    } else if (tok.startsWith('[')) {
      const mm = /\[([^\]]+)\]\(([^)\s]+)\)/.exec(tok)
      if (mm) {
        out.push(<a key={key} href={mm[2]} target="_blank" rel="noreferrer" className="text-[#4C9DFF] underline decoration-[#4C9DFF]/40">{mm[1]}</a>)
      } else out.push(tok)
    } else {
      out.push(<em key={key} className="italic">{tok.slice(1, -1)}</em>)
    }
    last = m.index + tok.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

function CodeBlock({ code, lang }: { code: string; lang: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="my-2 overflow-hidden rounded-lg border border-[#2C3037] bg-[#15171B]">
      <div className="flex items-center justify-between border-b border-[#2C3037] px-2.5 py-1 text-[10px] text-[#9BA1AA]">
        <span className="font-mono">{lang || 'texto'}</span>
        <button
          onClick={() => { void navigator.clipboard.writeText(code).catch(() => {}); setCopied(true); setTimeout(() => setCopied(false), 1200) }}
          className="flex items-center gap-1 hover:text-[#EDEEF0]"
        >
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />} {copied ? 'copiado' : 'copiar'}
        </button>
      </div>
      <pre className="max-h-80 overflow-auto px-3 py-2 text-xs"><code className="font-mono text-[#C9D2E0]">{code}</code></pre>
    </div>
  )
}

export function Markdown({ text }: { text: string }) {
  const lines = (text || '').replace(/\r\n/g, '\n').split('\n')
  const blocks: any[] = []
  let i = 0
  let key = 0
  while (i < lines.length) {
    const line = lines[i]
    const fence = /^```([\w+-]*)\s*$/.exec(line.trim())
    if (fence) {
      const lang = fence[1] || ''
      const buf: string[] = []
      i++
      while (i < lines.length && !/^```\s*$/.test(lines[i].trim())) { buf.push(lines[i]); i++ }
      i++
      blocks.push(<CodeBlock key={key++} code={buf.join('\n')} lang={lang} />)
      continue
    }
    const h = /^(#{1,3})\s+(.*)$/.exec(line)
    if (h) {
      const level = h[1].length
      const cls = level === 1 ? 'text-base font-semibold' : level === 2 ? 'text-sm font-semibold' : 'text-xs font-semibold uppercase tracking-wide text-[#9BA1AA]'
      blocks.push(<div key={key++} className={`mt-1 mb-1.5 text-[#EDEEF0] ${cls}`}>{inline(h[2], `h${key}`)}</div>)
      i++
      continue
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ''))
        i++
      }
      blocks.push(
        <ul key={key++} className="my-1 list-disc space-y-0.5 pl-5">
          {items.map((it, n) => <li key={n}>{inline(it, `li${key}-${n}`)}</li>)}
        </ul>,
      )
      continue
    }
    if (!line.trim()) { i++; continue }
    blocks.push(<p key={key++} className="my-1 whitespace-pre-wrap break-words">{inline(line, `p${key}`)}</p>)
    i++
  }
  return <>{blocks}</>
}
