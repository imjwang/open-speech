/** Which icon stands for what. One place, so a kind of thing always looks the same across the app. */
import {
  ALargeSmall,
  AudioLines,
  Braces,
  Brackets,
  Captions,
  Code,
  FileText,
  FileType2,
  Gauge,
  Globe,
  Hash,
  Image as ImageIcon,
  Languages,
  Link2,
  Palette,
  PanelBottom,
  PanelTop,
  Parentheses,
  Quote,
  Sigma,
  Superscript,
  Table,
  WandSparkles,
  type LucideIcon,
} from 'lucide-react'

/** A document by its kind (`DocumentOut.kind`). */
export function documentIcon(kind: string | undefined | null): LucideIcon {
  switch (kind) {
    case 'web':
      return Globe
    case 'image':
      return ImageIcon
    case 'docx':
      return FileType2
    default:
      return FileText
  }
}

/** The auto-skip presets (the engine's preset names). */
export const PRESET_ICONS: Record<string, LucideIcon> = {
  Headers: PanelTop,
  Footers: PanelBottom,
  'Page numbers': Hash,
  Footnotes: Superscript,
  Citations: Quote,
  URLs: Link2,
  Parentheses,
  Brackets,
  Braces,
  Tables: Table,
  Formulas: Sigma,
  Captions,
  Code,
}

/** The settings shown in the settings card (dotted keys). */
export const SETTING_ICONS: Record<string, LucideIcon> = {
  'appearance.theme': Palette,
  language: Languages,
  'defaults.voice': AudioLines,
  'defaults.speed': Gauge,
  'defaults.mode': WandSparkles,
  'reader.font_size': ALargeSmall,
}
