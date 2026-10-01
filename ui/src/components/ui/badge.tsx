import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const badgeVariants = cva('inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors', {
  variants: {
    variant: {
      default: 'bg-primary text-primary-foreground',
      secondary: 'bg-[#2C3037] text-foreground',
      outline: 'border border-[#3A4049] text-muted-foreground',
      success: 'bg-[#3DDC84]/15 text-[#3DDC84] border border-[#3DDC84]/20',
      warning: 'bg-amber-500/15 text-amber-400 border border-amber-500/20',
      muted: 'bg-[#22262D] text-muted-foreground',
    },
  },
  defaultVariants: { variant: 'default' },
})

export function Badge({ className, variant, ...props }: React.HTMLAttributes<HTMLDivElement> & VariantProps<typeof badgeVariants>) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}
