/**
 * Menu-bar icon (09 § Menu bar): idle (outline), listening (filled + pulse), playing (waveform),
 * attention (dot). Click opens the command bar; the menu has Play/Pause · Recent ▸ · Rules… · Quit, in the
 * app's language. The pulse is disabled under reduced motion.
 */
import path from 'node:path'

import { Menu, nativeImage, systemPreferences, Tray, type MenuItemConstructorOptions, type NativeImage } from 'electron'

import { t } from '../shared/i18n'
import { trayTooltip, trayVisual, type TrayInputs, type TrayVisual } from './tray-state'

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
  private inputs: TrayInputs = { listening: false, playing: false, attention: false }
  private detail: string | null = null
  private visual: TrayVisual = 'idle'
  private pulseTimer: ReturnType<typeof setInterval> | null = null
  private pulseOn = false

  constructor(
    private readonly iconsDir: string,
    private readonly actions: TrayActions,
  ) {}

  create(): void {
    if (this.tray) return
    this.tray = new Tray(this.image('idle'))
    this.tray.setToolTip(trayTooltip('idle', null))
    this.tray.on('click', () => this.actions.openBar())
    this.tray.on('right-click', () => void this.popUpMenu())
    this.render()
  }

  update(patch: Partial<TrayInputs>, detail?: string | null): void {
    this.inputs = { ...this.inputs, ...patch }
    if (detail !== undefined) this.detail = detail
    this.render()
  }

  get state(): TrayInputs {
    return this.inputs
  }

  destroy(): void {
    if (this.pulseTimer) clearInterval(this.pulseTimer)
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
    if (!this.tray) return
    const visual = trayVisual(this.inputs)
    this.visual = visual
    this.tray.setToolTip(trayTooltip(visual, this.detail))
    const pulse = visual === 'listening' && !prefersReducedMotion()
    if (pulse && !this.pulseTimer) {
      this.pulseTimer = setInterval(() => {
        this.pulseOn = !this.pulseOn
        if (this.visual === 'listening') this.tray?.setImage(this.image(this.pulseOn ? 'listening-pulse' : 'listening'))
      }, 700)
    } else if (!pulse && this.pulseTimer) {
      clearInterval(this.pulseTimer)
      this.pulseTimer = null
      this.pulseOn = false
    }
    this.tray.setImage(this.image(visual))
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
    const template: MenuItemConstructorOptions[] = [
      { label: t('tray.open'), click: () => this.actions.openBar() },
      { label: this.inputs.playing ? t('tray.pause') : t('tray.play'), click: () => this.actions.playPause() },
      { label: t('tray.recent'), submenu: recentItems },
      { label: t('tray.rules'), click: () => this.actions.openRules() },
      { type: 'separator' },
      { label: t('tray.quit'), role: 'quit', click: () => this.actions.quit() },
    ]
    this.tray.popUpContextMenu(Menu.buildFromTemplate(template))
  }
}
