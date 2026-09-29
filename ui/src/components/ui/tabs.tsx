import * as React from 'react'
import { cn } from '@/lib/utils'

type TabsContextType = { value: string; setValue: (v: string) => void }
const TabsCtx = React.createContext<TabsContextType | null>(null)

export function Tabs({ defaultValue, value, onValueChange, children, className }: { defaultValue?: string; value?: string; onValueChange?: (v: string) => void; children: React.ReactNode; className?: string }) {
  const [internal, setInternal] = React.useState(defaultValue ?? '')
  const v = value ?? internal
  const setValue = (nv: string) => { if (!value) setInternal(nv); onValueChange?.(nv) }
  return <TabsCtx.Provider value={{ value: v, setValue }}><div className={cn(className)}>{children}</div></TabsCtx.Provider>
}
export function TabsList({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('inline-flex items-center gap-1 rounded-xl bg-white/[0.06] p-1', className)} {...props} />
}
export function TabsTrigger({ value, children, className }: { value: string; children: React.ReactNode; className?: string }) {
  const ctx = React.useContext(TabsCtx)!
  const active = ctx.value === value
  return (
    <button onClick={() => ctx.setValue(value)} className={cn('px-3 py-1.5 text-sm rounded-lg transition-all font-medium', active ? 'bg-white text-zinc-900 shadow' : 'text-muted-foreground hover:text-foreground', className)}>
      {children}
    </button>
  )
}
export function TabsContent({ value, children, className }: { value: string; children: React.ReactNode; className?: string }) {
  const ctx = React.useContext(TabsCtx)!
  if (ctx.value !== value) return null
  return <div className={cn(className)}>{children}</div>
}
