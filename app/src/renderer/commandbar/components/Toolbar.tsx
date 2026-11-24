import { AudioLines, ClipboardList, History, ListChecks, Paperclip, SlidersHorizontal } from 'lucide-react'
import type { ReactNode } from 'react'

import { IconButton } from '@/components/ui/icon-button'
import { useT } from '@/i18n'
import type { Show } from '../actions'

export interface ToolbarHandlers {
  attach(): void
  clipboard(): void
  show(what: Show): void
  rules(): void
}

/**
 * Everything the bar can do without a command, as icons: attach, read the clipboard, recent documents,
 * voices, rules, settings; and at the end (`trailing`), this month's spending. Each has a name on hover and
 * for screen readers; all of them can also be asked for by voice.
 */
export function Toolbar({ handlers, trailing }: { handlers: ToolbarHandlers; trailing?: ReactNode }) {
  const t = useT()
  return (
    <div role="toolbar" aria-label={t('app.name')} className="flex items-center gap-0.5 border-t px-3 py-1.5" data-testid="toolbar">
      <IconButton label={t('tool.attach')} shortcut="@" onClick={handlers.attach} tabIndex={-1}>
        <Paperclip />
      </IconButton>
      <IconButton label={t('tool.clipboard')} onClick={handlers.clipboard} tabIndex={-1}>
        <ClipboardList />
      </IconButton>
      <IconButton label={t('tool.recents')} onClick={() => handlers.show('recents')} tabIndex={-1}>
        <History />
      </IconButton>
      <span aria-hidden="true" className="mx-1 h-4 w-px bg-border" />
      <IconButton label={t('tool.voices')} onClick={() => handlers.show('voices')} tabIndex={-1}>
        <AudioLines />
      </IconButton>
      <IconButton label={t('tool.rules')} onClick={handlers.rules} tabIndex={-1}>
        <ListChecks />
      </IconButton>
      <IconButton label={t('tool.settings')} onClick={() => handlers.show('settings')} tabIndex={-1}>
        <SlidersHorizontal />
      </IconButton>
      <span className="ml-auto flex items-center gap-2">{trailing}</span>
    </div>
  )
}
