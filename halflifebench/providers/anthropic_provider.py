from __future__ import annotations

import logging
from typing import List

from anthropic import Anthropic

from .base import CompletionResult, Message

logger = logging.getLogger(__name__)


class AnthropicProvider:
    def __init__(self) -> None:
        self.client = Anthropic(timeout=60.0, max_retries=2)

    def complete(
        self,
        *,
        model: str,
        messages: List[Message],
        temperature: float = 0.0,
        seed: int | None = None,  # Anthropic messages API does not currently use seed.
        max_output_tokens: int = 700,
    ) -> CompletionResult:
        del seed
        logger.debug(
            "Anthropic complete start model=%s messages=%d max_output_tokens=%d temperature=%.3f",
            model,
            len(messages),
            max_output_tokens,
            temperature,
        )

        system_parts: List[str] = []
        converted_messages: List[dict] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in {"system", "developer"}:
                system_parts.append(content)
            elif role in {"user", "assistant"}:
                converted_messages.append({"role": role, "content": content})
            else:
                converted_messages.append({"role": "user", "content": content})

        if not converted_messages:
            converted_messages = [{"role": "user", "content": ""}]

        system_blocks = [{"type": "text", "text": s} for s in system_parts] if system_parts else []
        logger.debug(
            "Anthropic message conversion complete system_blocks=%d conversation_messages=%d",
            len(system_blocks),
            len(converted_messages),
        )

        response = self.client.messages.create(
            model=model,
            system=system_blocks,
            messages=converted_messages,
            max_tokens=max_output_tokens,
            temperature=temperature,
        )

        text_parts: List[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", ""))
        content = "".join(text_parts)

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        metadata = {
            "id": getattr(response, "id", None),
            "stop_reason": getattr(response, "stop_reason", None),
        }
        logger.debug(
            "Anthropic response received model=%s input_tokens=%d output_tokens=%d response_id=%s",
            model,
            input_tokens,
            output_tokens,
            metadata.get("id"),
        )
        return CompletionResult(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            metadata=metadata,
        )
