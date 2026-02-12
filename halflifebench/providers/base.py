from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol, TypedDict


Message = Dict[str, str]


class BatchRequest(TypedDict, total=False):
    custom_id: str
    model: str
    messages: List[Message]
    temperature: float
    seed: int | None
    max_output_tokens: int
    reasoning_effort: str


@dataclass
class CompletionResult:
    content: str
    input_tokens: int
    output_tokens: int
    model: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class ModelProvider(Protocol):
    def complete(
        self,
        *,
        model: str,
        messages: List[Message],
        temperature: float = 0.0,
        seed: int | None = None,
        max_output_tokens: int = 700,
        reasoning_effort: str = "medium",
        max_empty_retries: int = 0,
    ) -> CompletionResult:
        ...

    def complete_batch(
        self,
        *,
        requests: List[BatchRequest],
        poll_interval: int = 60,
    ) -> Dict[str, CompletionResult]:
        ...
