import type * as React from 'react'

import { cn } from '@/lib/utils'
import { Tooltip, TooltipContent, TooltipTrigger } from './tooltip'

/**
 * A control that is an icon. Its name is always there for assistive technology (`aria-label`) and on hover
 * or keyboard focus (a tooltip), so the interface can stay free of text without becoming a guessing game.
 */
export function IconButton({
  label,
  shortcut,
  active,
  className,
  children,
  side = 'top',
  ...props
}: Omit<React.ComponentProps<'button'>, 'aria-label'> & {
  label: string
  /** Shown beside the name in the tooltip, e.g. "⌘K". */
  shortcut?: string
  active?: boolean
  side?: 'top' | 'bottom' | 'left' | 'right'
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={label}
          aria-pressed={active}
          data-active={active || undefined}
          className={cn(
            'inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors outline-none',
            'hover:bg-accent hover:text-accent-foreground focus-visible:ring-2 focus-visible:ring-ring',
            'disabled:pointer-events-none disabled:opacity-40 data-[active]:bg-accent data-[active]:text-primary',
            "[&_svg:not([class*='size-'])]:size-[18px]",
            className,
          )}
          {...props}
        >
          {children}
        </button>
      </TooltipTrigger>
      <TooltipContent side={side}>
        {label}
        {shortcut ? <span className="ml-2 opacity-70">{shortcut}</span> : null}
      </TooltipContent>
    </Tooltip>
  )
}
