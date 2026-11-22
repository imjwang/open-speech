"""Rule model (06 § Rule file format, § Compiled filters).

A rule is one Markdown file: YAML frontmatter (name, description, scope, enabled, tier, compiled,
created_by) and a body holding the natural-language instruction. `Rule.id` is derived from the file name
(`rule_<name>`), so it is stable across reloads and edits.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from open_speech.api.schemas import RuleOut, RuleScope
from open_speech.document.model import BlockType, MarkKind
from open_speech.providers.base import RuleBrief

Tier = Literal["filter", "annotate", "transform"]

MAX_PATTERN_LEN = 500
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def slugify(text: str, *, max_len: int = 64) -> str:
    """A rule/file name: lowercase `[a-z0-9-]`, 1–64 chars."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:max_len].strip("-")
    return s or "rule"


def rule_id_for(name: str) -> str:
    return f"rule_{name}"


def name_from_rule_id(rule_id: str) -> str:
    return rule_id[5:] if rule_id.startswith("rule_") else rule_id


class ReplaceSpec(BaseModel):
    """Regex → replacement, applied to the text that remains after drops (06 § Compiled filters)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    pattern: str
    with_: str = Field("", alias="with")

    @field_validator("pattern")
    @classmethod
    def _valid_regex(cls, v: str) -> str:
        if not v:
            raise ValueError("pattern must not be empty")
        if len(v) > MAX_PATTERN_LEN:
            raise ValueError(f"pattern longer than {MAX_PATTERN_LEN} chars")
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from e
        return v


class CompiledFilter(BaseModel):
    """Machine-managed filter spec (tier 1). `keep_blocks`/`keep_marks` are additions to 06: an explicit
    "read X" that overrides a lower-precedence "skip X" (and is reported as a conflict)."""

    model_config = ConfigDict(extra="forbid")

    drop_blocks: list[BlockType] = Field(default_factory=list)
    drop_marks: list[MarkKind] = Field(default_factory=list)
    replace: list[ReplaceSpec] = Field(default_factory=list)
    keep_blocks_only: list[BlockType] | None = None
    keep_blocks: list[BlockType] = Field(default_factory=list)
    keep_marks: list[MarkKind] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.drop_blocks
            or self.drop_marks
            or self.replace
            or self.keep_blocks_only is not None
            or self.keep_blocks
            or self.keep_marks
        )

    def to_file_dict(self) -> dict[str, Any]:
        """The YAML shape written to rule files (06 keys always; additions only when used)."""
        out: dict[str, Any] = {
            "drop_blocks": [str(b) for b in self.drop_blocks],
            "drop_marks": [str(m) for m in self.drop_marks],
            "replace": [{"pattern": r.pattern, "with": r.with_} for r in self.replace],
            "keep_blocks_only": (
                [str(b) for b in self.keep_blocks_only] if self.keep_blocks_only is not None else None
            ),
        }
        if self.keep_blocks:
            out["keep_blocks"] = [str(b) for b in self.keep_blocks]
        if self.keep_marks:
            out["keep_marks"] = [str(m) for m in self.keep_marks]
        return out


class Rule(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    name: str
    description: str = ""
    instruction: str = ""
    scope: RuleScope = Field(default_factory=RuleScope)
    enabled: bool = True
    tier: Tier = "filter"
    compiled: CompiledFilter | None = None
    created_by: str = "user"
    preset: bool = False
    path: Path | None = None
    updated_at: float = 0.0  # file mtime (seconds since epoch)
    # Addition: a hand-written rule with no `tier` that the deterministic compiler couldn't express.
    # The service should run `compile_rule` with Claude and save the result.
    needs_compile: bool = False

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("rule name must be 1–64 chars of [a-z0-9-]")
        return v

    @classmethod
    def new(
        cls,
        name: str,
        *,
        instruction: str,
        description: str = "",
        scope: RuleScope | None = None,
        tier: Tier = "filter",
        compiled: CompiledFilter | None = None,
        created_by: str = "user",
        enabled: bool = True,
    ) -> Rule:
        slug = slugify(name)
        return cls(
            id=rule_id_for(slug),
            name=slug,
            description=description,
            instruction=instruction,
            scope=scope or RuleScope(),
            tier=tier,
            compiled=compiled,
            created_by=created_by,
            preset=created_by == "preset",
            enabled=enabled,
        )

    @property
    def is_filter(self) -> bool:
        return self.tier == "filter" and self.compiled is not None

    def brief(self) -> RuleBrief:
        return RuleBrief(id=self.id, name=self.name, instruction=self.instruction or self.description)

    def to_out(self) -> RuleOut:
        return RuleOut(
            id=self.id,
            name=self.name,
            description=self.description,
            instruction=self.instruction,
            scope=self.scope,
            tier=self.tier,
            enabled=self.enabled,
            created_by=self.created_by,
            compiled=self.compiled.to_file_dict() if self.compiled is not None else None,
            preset=self.preset,
            path=str(self.path) if self.path else None,
            updated_at=(datetime.fromtimestamp(self.updated_at, UTC).isoformat() if self.updated_at else None),
        )

    @classmethod
    def from_out(cls, out: RuleOut) -> Rule:
        """Addition: rebuild a `Rule` from its API shape (e.g. `SaveRuleRequest.rule`)."""
        name = slugify(out.name)
        return cls(
            id=rule_id_for(name),
            name=name,
            description=out.description,
            instruction=out.instruction,
            scope=out.scope,
            tier=out.tier,
            enabled=out.enabled,
            created_by=out.created_by,
            compiled=CompiledFilter.model_validate(out.compiled) if out.compiled is not None else None,
            preset=out.preset or out.created_by == "preset",
            path=Path(out.path) if out.path else None,
        )
