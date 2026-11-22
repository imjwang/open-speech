"""Engine errors → RFC 9457 problem+json with stable codes (04 § Conventions)."""

from __future__ import annotations

from typing import Any, Literal

ErrorCode = Literal[
    "missing_key",
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
    "missing_key": 424,
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


class MissingKey(EngineError):
    def __init__(self, provider: str, feature: str = "this") -> None:
        names = {"anthropic": "Anthropic", "openai": "OpenAI", "gemini": "Gemini"}
        super().__init__(
            "missing_key",
            f"{feature.capitalize()} needs an {names.get(provider, provider)} key. "
            "Paste one into the bar, or keep going with the local voice.",
            provider=provider,
        )


class BudgetExceeded(EngineError):
    def __init__(self, spent: str, budget: str) -> None:
        super().__init__(
            "budget_exceeded",
            f"You've reached your ${budget} monthly budget. I'll keep reading with the local voice; "
            "say 'raise my budget' to change it.",
            spent=spent,
            budget=budget,
        )


class EstimateRequired(EngineError):
    def __init__(self, usd_estimate: str, operation: str) -> None:
        super().__init__(
            "estimate_required",
            f"That will cost about ${usd_estimate}. Confirm to go ahead.",
            usd_estimate=usd_estimate,
            operation=operation,
        )


class NotFound(EngineError):
    def __init__(self, what: str) -> None:
        super().__init__("not_found", f"I couldn't find {what}.")
