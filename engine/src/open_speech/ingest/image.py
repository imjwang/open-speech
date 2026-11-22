"""Images → speech (FR-MM-1, 05 § 5): downscale to ≤ 1280 px on the long edge, then one Claude vision call
that transcribes (text-heavy), describes (visual) or both, under the active rules. The vision call has no
tools; its output is validated and treated as content."""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import ValidationError

from open_speech.document.model import BlockType, Document, SourceKind
from open_speech.errors import EngineError, MissingKey
from open_speech.ingest.common import DraftBlock, build_document, finalize_blocks, infer_title, source_id_bytes
from open_speech.providers.base import LlmCall, RuleBrief, VisionBlock, VisionProvider

MAX_EDGE = 1280
_JPEG_OVER = 1_500_000  # bytes: photos re-encode as JPEG above this size

_VISION_TYPES: dict[str, BlockType] = {
    "paragraph": BlockType.paragraph,
    "heading": BlockType.heading,
    "list_item": BlockType.list_item,
    "table": BlockType.table,
    "caption": BlockType.caption,
    "figure": BlockType.figure,
}


def sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/heic"
    return None


def prepare_image(data: bytes) -> tuple[bytes, str]:
    """Downscale to ≤ 1280 px long edge and encode as PNG (or JPEG for large opaque photos)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as opened:
            im = ImageOps.exif_transpose(opened)
            im.load()
    except (UnidentifiedImageError, OSError) as e:
        raise EngineError("invalid_request", "That image couldn't be opened.") from e
    if max(im.size) > MAX_EDGE:
        im.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if alpha else "RGB")
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    if not alpha and buf.tell() > _JPEG_OVER:
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        return buf.getvalue(), "image/jpeg"
    return buf.getvalue(), "image/png"


def vision_drafts(output: Any) -> list[DraftBlock]:
    """Coerce vision output (VisionBlocks, dicts, (type, text) tuples or plain text) into draft blocks."""
    items: list[Any] = list(output) if isinstance(output, list | tuple) else [output]
    drafts: list[DraftBlock] = []
    for item in items:
        vb: VisionBlock | None = None
        if isinstance(item, VisionBlock):
            vb = item
        elif isinstance(item, dict):
            try:
                vb = VisionBlock.model_validate(item)
            except ValidationError:
                vb = None
        elif isinstance(item, tuple) and len(item) == 2:
            try:
                vb = VisionBlock(type=item[0], text=str(item[1]))
            except ValidationError:
                vb = VisionBlock(type="paragraph", text=str(item[1]))
        elif isinstance(item, str):
            for para in item.split("\n\n"):
                if para.strip():
                    drafts.append(DraftBlock(type=BlockType.paragraph, text=" ".join(para.split())))
            continue
        if vb is None or not vb.text.strip():
            continue
        btype = _VISION_TYPES.get(vb.type, BlockType.paragraph)
        text = vb.text.strip() if btype == BlockType.table else " ".join(vb.text.split())
        drafts.append(DraftBlock(type=btype, text=text, level=1 if btype == BlockType.heading else None))
    return drafts


async def ingest_image(
    data: bytes,
    *,
    vision: VisionProvider | None,
    rules: Sequence[RuleBrief] = (),
    mode: Literal["auto", "transcribe", "describe"] = "auto",
    title: str | None = None,
    filename: str | None = None,
    extractor_version: str,
) -> tuple[Document, LlmCall]:
    if vision is None:
        raise MissingKey("anthropic", "Describing images")
    image, media_type = prepare_image(data)
    call = await vision.describe_or_transcribe(image, media_type, list(rules), mode)
    drafts = vision_drafts(call.output)
    if not drafts:
        raise EngineError("provider_error", "I couldn't make out anything in that image.")
    source_id = source_id_bytes(data)
    image_id = f"img_{source_id[:12]}"
    for d in drafts:
        d.image_id = image_id
    blocks = finalize_blocks(drafts)
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0] if filename else None
    doc = build_document(
        source_id=source_id,
        kind=SourceKind.image,
        blocks=blocks,
        title=infer_title(blocks, explicit=title, fallback=stem or "Image"),
        extractor_version=extractor_version,
    )
    return doc, call
