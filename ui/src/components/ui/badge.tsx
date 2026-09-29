import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const badgeVariants = cva('inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors', {
  variants: {
    variant: {
      default: 'bg-primary text-primary-foreground',
      secondary: 'bg-white/10 text-foreground',
      outline: 'border border-white/15 text-muted-foreground',
      success: 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/20',
      warning: 'bg-amber-500/15 text-amber-400 border border-amber-500/20',
      muted: 'bg-white/[0.06] text-muted-foreground',
    },
  },
  defaultVariants: { variant: 'default' },
})

export function Badge({ className, variant, ...props }: React.HTMLAttributes<HTMLDivElement> & VariantProps<typeof badgeVariants>) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}
