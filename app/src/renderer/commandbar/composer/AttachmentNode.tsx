/**
 * An attachment as an inline badge in the editor. It is one atomic thing: the caret steps over it, Backspace
 * removes it whole, and its ✕ removes it with the pointer.
 */
import { $getNodeByKey, DecoratorNode, type LexicalEditor, type LexicalNode, type NodeKey, type SerializedLexicalNode } from 'lexical'
import { AlignLeft, FileText, FileType2, Globe, Image as ImageIcon, X } from 'lucide-react'
import type { JSX } from 'react'

import { useT } from '@/i18n'
import { badgeKind, labelOf, textOf, type AttachmentItem } from './attachments'

type SerializedAttachmentNode = SerializedLexicalNode & { label: string }

function Icon({ item }: { item: AttachmentItem }): JSX.Element {
  if (item.kind === 'url') return <Globe aria-hidden="true" />
  if (item.kind === 'text') return <AlignLeft aria-hidden="true" />
  if (item.fileKind === 'image') return <ImageIcon aria-hidden="true" />
  if (item.fileKind === 'doc') return <FileType2 aria-hidden="true" />
  return <FileText aria-hidden="true" />
}

export function AttachmentChip({ item, onRemove }: { item: AttachmentItem; onRemove?: () => void }): JSX.Element {
  const t = useT()
  const label = labelOf(item)
  const title = item.kind === 'url' ? item.url : item.kind === 'file' ? item.path : item.kind === 'text' ? item.text.slice(0, 400) : item.name
  return (
    <span className="badge-chip group/chip" data-kind={badgeKind(item)} data-testid="attachment" title={title}>
      <Icon item={item} />
      <span>{label}</span>
      {onRemove && (
        <button
          type="button"
          tabIndex={-1}
          aria-label={t('attach.remove', { name: label })}
          className="-mr-1 hidden rounded-full p-px opacity-70 hover:opacity-100 group-hover/chip:inline-flex"
          onMouseDown={(e) => e.preventDefault()}
          onClick={onRemove}
        >
          <X className="size-3" aria-hidden="true" />
        </button>
      )}
    </span>
  )
}

export class AttachmentNode extends DecoratorNode<JSX.Element> {
  __item: AttachmentItem

  static override getType(): string {
    return 'attachment'
  }

  static override clone(node: AttachmentNode): AttachmentNode {
    return new AttachmentNode(node.__item, node.__key)
  }

  static override importJSON(): AttachmentNode {
    // Attachments hold live references (a File, a path the user chose): they are never restored from JSON.
    return new AttachmentNode({ kind: 'text', text: '' })
  }

  constructor(item: AttachmentItem, key?: NodeKey) {
    super(key)
    this.__item = item
  }

  getItem(): AttachmentItem {
    return this.getLatest().__item
  }

  override exportJSON(): SerializedAttachmentNode {
    return { ...super.exportJSON(), type: 'attachment', version: 1, label: labelOf(this.__item) }
  }

  override createDOM(): HTMLElement {
    const span = document.createElement('span')
    span.style.display = 'inline-block'
    return span
  }

  override updateDOM(): false {
    return false
  }

  /** In plain text (copy, and the command's text) a badge reads as what it stands for. */
  override getTextContent(): string {
    return textOf(this.__item)
  }

  override isInline(): true {
    return true
  }

  /** Arrows step over a badge instead of selecting it, so typing never lands "inside" one. */
  override isKeyboardSelectable(): false {
    return false
  }

  override decorate(editor: LexicalEditor): JSX.Element {
    const key = this.getKey()
    return <AttachmentChip item={this.__item} onRemove={() => editor.update(() => $getNodeByKey(key)?.remove())} />
  }
}

export function $createAttachmentNode(item: AttachmentItem): AttachmentNode {
  return new AttachmentNode(item)
}

export function $isAttachmentNode(node: LexicalNode | null | undefined): node is AttachmentNode {
  return node instanceof AttachmentNode
}
