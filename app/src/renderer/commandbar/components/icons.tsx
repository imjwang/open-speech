/** Which icon stands for what. One place, so a kind of thing always looks the same across the app. */
import {
  ALargeSmall,
  AudioLines,
  FileText,
  FileType2,
  Gauge,
  Globe,
  Image as ImageIcon,
  Cpu,
  Layers,
  Languages,
  Palette,
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

/** The settings shown in the settings card (dotted keys). */
export const SETTING_ICONS: Record<string, LucideIcon> = {
  'appearance.theme': Palette,
  language: Languages,
  'defaults.voice': AudioLines,
  'defaults.speed': Gauge,
  'reader.font_size': ALargeSmall,
  'qwen3.size': Layers,
  'qwen3.precision': Cpu,
}
