from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol


Message = Dict[str, str]


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
    ) -> CompletionResult:
        ...
