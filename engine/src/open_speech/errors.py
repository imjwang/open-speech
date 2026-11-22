"""Engine errors → RFC 9457 problem+json with stable codes (04 § Conventions)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from open_speech.i18n import t

ErrorCode = Literal[
    "unavailable",
    "budget_exceeded",
    "estimate_required",
    "provider_error",
    "rate_limited",
    "not_found",
    "invalid_rule",
    "offline",
    "invalid_request",
    "conflict",
]

_STATUS: dict[str, int] = {
    "unavailable": 424,
    "budget_exceeded": 402,
    "estimate_required": 409,
    "provider_error": 502,
    "rate_limited": 429,
    "not_found": 404,
    "invalid_rule": 422,
    "offline": 503,
    "invalid_request": 400,
    "conflict": 409,
}


class EngineError(Exception):
    """Raise anywhere in the engine; the API layer renders it as problem+json, the CLI prints `detail`,
    and the assistant speaks `detail` (FR-ASST-7: one sentence with the next step)."""

    def __init__(self, code: ErrorCode, detail: str, *, title: str | None = None, **extra: Any) -> None:
        super().__init__(detail)
        self.code: ErrorCode = code
        self.detail = detail
        self.title = title or code.replace("_", " ").capitalize()
        self.status = _STATUS.get(code, 500)
        self.extra = extra

    def problem(self) -> dict[str, Any]:
        return {
            "type": f"https://open-speech.dev/problems/{self.code}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "code": self.code,
            **self.extra,
        }


Feature = Literal["assistant", "ai_cleanup", "vision", "speech_input", "voice", "rule", "rule_preview"]


class Unavailable(EngineError):
    """A feature this build doesn't include: its provider was not configured when the app was built. Nothing
    the user can enter fixes it, so the message says what still works instead of asking for anything."""

    def __init__(self, feature: Feature, *, detail: str | None = None, provider: str | None = None) -> None:
        extra = {"provider": provider} if provider else {}
        super().__init__("unavailable", detail or t(f"unavailable.{feature}"), feature=feature, **extra)


class BudgetExceeded(EngineError):
    def __init__(self, spent: str, budget: str) -> None:
        super().__init__(
            "budget_exceeded",
            t("error.budget_exceeded", budget=budget),
            spent=spent,
            budget=budget,
        )


class EstimateRequired(EngineError):
    def __init__(self, usd_estimate: str, operation: str) -> None:
        super().__init__(
            "estimate_required",
            t(
                "error.estimate_required.read" if operation == "read" else "error.estimate_required",
                usd=f"{Decimal(usd_estimate):.2f}",
            ),
            usd_estimate=usd_estimate,
            operation=operation,
        )


class NotFound(EngineError):
    def __init__(self, what: str) -> None:
        super().__init__("not_found", t("error.not_found", what=what))
