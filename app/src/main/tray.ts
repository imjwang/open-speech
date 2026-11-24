/**
 * Menu-bar icon (09 § Menu bar): idle (outline), listening (filled + pulse), speaking (a speech bubble),
 * playing (waveform), attention (dot). Click opens the command bar; the menu starts with this month's spending
 * ("This month: $0.12 · estimated") and has Play/Pause · Recent ▸ · Rules… · Quit, in the app's language. The
 * amount can also sit beside the icon (off by default). The pulse is disabled under reduced motion. The image,
 * tooltip and title are set only when they change.
 */
import path from 'node:path'

import { Menu, nativeImage, systemPreferences, Tray, type MenuItemConstructorOptions, type NativeImage } from 'electron'

import { getLanguage, onLanguageChange, t } from '../shared/i18n'
import { formatSpend, traySpendLine, type BudgetState } from '../shared/spend'
import { trayUpdates, trayView, type TrayInputs, type TrayView } from './tray-state'

export interface RecentItem {
  id: string
  title: string
  progress: number
}

export interface TrayActions {
  openBar(): void
  playPause(): void
  playDocument(documentId: string): void
  loadRecents(): Promise<RecentItem[]>
  openRules(): void
  /** The "show spending in the menu bar" checkbox was toggled. */
  setShowSpend(show: boolean): void
  quit(): void
}

function prefersReducedMotion(): boolean {
  try {
    if (process.platform === 'darwin' || process.platform === 'win32') {
      return systemPreferences.getAnimationSettings().prefersReducedMotion
    }
  } catch {
    // unsupported
  }
  return false
}

export class TrayController {
  private tray: Tray | null = null
  private readonly images = new Map<string, NativeImage>()
  private inputs: TrayInputs = { listening: false, speaking: false, playing: false, attention: false }
  private detail: string | null = null
  private budget: BudgetState | null = null
  private showSpend = false
  /** What the menu-bar item shows right now. */
  private shown: TrayView | null = null
  private pulseTimer: ReturnType<typeof setInterval> | null = null
  private pulseOn = false
  private offLanguage: (() => void) | null = null

  constructor(
    private readonly iconsDir: string,
    private readonly actions: TrayActions,
  ) {}

  create(): void {
    if (this.tray) return
    this.tray = new Tray(this.image('idle'))
    this.shown = null
    this.tray.on('click', () => this.actions.openBar())
    this.tray.on('right-click', () => void this.popUpMenu())
    this.offLanguage = onLanguageChange(() => this.render())
    this.render()
  }

  update(patch: Partial<TrayInputs>, detail?: string | null): void {
    this.inputs = { ...this.inputs, ...patch }
    if (detail !== undefined) this.detail = detail
    this.render()
  }

  /** This month's spending (`GET /usage/budget`, `usage.changed`). */
  setBudget(budget: BudgetState | null): void {
    this.budget = budget
    this.render()
  }

  setShowSpend(show: boolean): void {
    this.showSpend = show
    this.render()
  }

  get state(): TrayInputs {
    return this.inputs
  }

  destroy(): void {
    if (this.pulseTimer) clearInterval(this.pulseTimer)
    this.pulseTimer = null
    this.offLanguage?.()
    this.tray?.destroy()
    this.tray = null
  }

  private image(name: string): NativeImage {
    let img = this.images.get(name)
    if (!img) {
      img = nativeImage.createFromPath(path.join(this.iconsDir, `${name}Template.png`))
      img.setTemplateImage(true)
      this.images.set(name, img)
    }
    return img
  }

  private render(): void {
    const tray = this.tray
    if (!tray) return
    const lang = getLanguage()
    const next = trayView(this.inputs, {
      detail: this.detail,
      pulseOn: this.pulseOn,
      spendLine: traySpendLine(this.budget, lang),
      title: this.showSpend && this.budget ? formatSpend(this.budget.spent_usd, lang) : '',
    })
    const changes = trayUpdates(this.shown, next)
    if (changes.image !== undefined) tray.setImage(this.image(changes.image))
    if (changes.tooltip !== undefined) tray.setToolTip(changes.tooltip)
    if (changes.title !== undefined) tray.setTitle(changes.title, { fontType: 'monospacedDigit' })
    this.shown = next
    this.syncPulse(next.image === 'listening' || next.image === 'listening-pulse')
  }

  /** The listening pulse alternates two frames; other updates leave whichever frame is showing alone. */
  private syncPulse(listening: boolean): void {
    const pulse = listening && !prefersReducedMotion()
    if (pulse && !this.pulseTimer) {
      this.pulseTimer = setInterval(() => {
        this.pulseOn = !this.pulseOn
        this.render()
      }, 700)
    } else if (!pulse && this.pulseTimer) {
      clearInterval(this.pulseTimer)
      this.pulseTimer = null
      if (this.pulseOn) {
        this.pulseOn = false
        this.render()
      }
    }
  }

  private async popUpMenu(): Promise<void> {
    if (!this.tray) return
    let recents: RecentItem[] = []
    try {
      recents = await this.actions.loadRecents()
    } catch {
      recents = []
    }
    const recentItems: MenuItemConstructorOptions[] = recents.length
      ? recents.slice(0, 10).map((r) => ({
          label: `${r.title}${r.progress > 0 ? ` · ${Math.round(r.progress * 100)}%` : ''}`,
          click: () => this.actions.playDocument(r.id),
        }))
      : [{ label: t('tray.nothing_recent'), enabled: false }]
    const spendLine = traySpendLine(this.budget, getLanguage())
    const template: MenuItemConstructorOptions[] = [
      ...(spendLine ? [{ label: spendLine, enabled: false }, { type: 'separator' as const }] : []),
      { label: t('tray.open'), click: () => this.actions.openBar() },
      { label: this.inputs.playing ? t('tray.pause') : t('tray.play'), click: () => this.actions.playPause() },
      { label: t('tray.recent'), submenu: recentItems },
      { label: t('tray.rules'), click: () => this.actions.openRules() },
      { type: 'separator' },
      {
        label: t('tray.spend_in_menu_bar'),
        type: 'checkbox',
        checked: this.showSpend,
        click: (item) => this.actions.setShowSpend(item.checked),
      },
      { type: 'separator' },
      { label: t('tray.quit'), role: 'quit', click: () => this.actions.quit() },
    ]
    this.tray.popUpContextMenu(Menu.buildFromTemplate(template))
  }
}
