from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Tuple

from anthropic import Anthropic

from .base import BatchRequest, CompletionResult, Message

logger = logging.getLogger(__name__)


class AnthropicProvider:
    def __init__(self) -> None:
        self.client = Anthropic(timeout=60.0, max_retries=2)

    @staticmethod
    def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def _extract_text_from_content(content_blocks: Any) -> str:
        if not isinstance(content_blocks, list):
            return ""
        text_parts: List[str] = []
        for block in content_blocks:
            block_type = AnthropicProvider._obj_get(block, "type")
            if block_type == "text":
                text_parts.append(str(AnthropicProvider._obj_get(block, "text", "")))
        return "".join(text_parts)

    def _convert_messages(self, messages: List[Message]) -> Tuple[List[dict], List[dict]]:
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
        return system_blocks, converted_messages

    def complete(
        self,
        *,
        model: str,
        messages: List[Message],
        temperature: float = 0.0,
        seed: int | None = None,  # Anthropic messages API does not currently use seed.
        max_output_tokens: int = 700,
        reasoning_effort: str = "medium",
        max_empty_retries: int = 0,
    ) -> CompletionResult:
        del seed
        del reasoning_effort
        del max_empty_retries
        logger.debug(
            "Anthropic complete start model=%s messages=%d max_output_tokens=%d temperature=%.3f",
            model,
            len(messages),
            max_output_tokens,
            temperature,
        )

        system_blocks, converted_messages = self._convert_messages(messages)
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

        content = self._extract_text_from_content(getattr(response, "content", []))

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

    def complete_batch(
        self,
        *,
        requests: List[BatchRequest],
        poll_interval: int = 60,
    ) -> Dict[str, CompletionResult]:
        if not requests:
            return {}

        safe_poll_interval = max(1, int(poll_interval))
        expected_ids: set[str] = set()
        expected_models: Dict[str, str] = {}
        batch_requests: List[Dict[str, Any]] = []
        for req in requests:
            custom_id = str(req.get("custom_id", "")).strip()
            model = str(req.get("model", "")).strip()
            messages = req.get("messages")
            if not custom_id:
                raise ValueError("Anthropic batch request missing custom_id")
            if custom_id in expected_ids:
                raise ValueError(f"Duplicate Anthropic batch custom_id: {custom_id}")
            if not model:
                raise ValueError(f"Anthropic batch request {custom_id} missing model")
            if not isinstance(messages, list):
                raise ValueError(f"Anthropic batch request {custom_id} missing messages list")

            system_blocks, converted_messages = self._convert_messages(messages)
            params: Dict[str, Any] = {
                "model": model,
                "system": system_blocks,
                "messages": converted_messages,
                "max_tokens": int(req.get("max_output_tokens", 700)),
            }
            if "temperature" in req and req.get("temperature") is not None:
                params["temperature"] = float(req.get("temperature"))
            batch_requests.append({"custom_id": custom_id, "params": params})
            expected_ids.add(custom_id)
            expected_models[custom_id] = model

        batch = self.client.messages.batches.create(requests=batch_requests)
        batch_id = self._obj_get(batch, "id")
        logger.info("Anthropic batch submitted batch_id=%s requests=%d", batch_id, len(batch_requests))

        while True:
            batch = self.client.messages.batches.retrieve(batch_id)
            status = str(self._obj_get(batch, "processing_status", "unknown"))
            counts = self._obj_get(batch, "request_counts")
            logger.info("Anthropic batch poll batch_id=%s status=%s counts=%s", batch_id, status, counts)
            if status == "ended":
                break
            time.sleep(safe_poll_interval)

        results: Dict[str, CompletionResult] = {}
        errors: Dict[str, Any] = {}
        for item in self.client.messages.batches.results(batch_id):
            custom_id = str(self._obj_get(item, "custom_id", "")).strip()
            if not custom_id:
                continue
            result_obj = self._obj_get(item, "result")
            result_type = str(self._obj_get(result_obj, "type", ""))
            if result_type != "succeeded":
                errors[custom_id] = {
                    "type": result_type,
                    "error": self._obj_get(result_obj, "error"),
                }
                continue

            message_obj = self._obj_get(result_obj, "message")
            content = self._extract_text_from_content(self._obj_get(message_obj, "content", []))
            usage = self._obj_get(message_obj, "usage")
            input_tokens = int(self._obj_get(usage, "input_tokens", 0) or 0)
            output_tokens = int(self._obj_get(usage, "output_tokens", 0) or 0)
            model_name = str(self._obj_get(message_obj, "model", "")) or expected_models.get(custom_id, "")
            metadata = {
                "id": self._obj_get(message_obj, "id"),
                "stop_reason": self._obj_get(message_obj, "stop_reason"),
                "batch_result_type": result_type,
            }
            results[custom_id] = CompletionResult(
                content=content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model_name,
                metadata=metadata,
            )

        missing = sorted(expected_ids - set(results.keys()))
        if missing or errors:
            if errors:
                for custom_id, error_payload in errors.items():
                    logger.error("Anthropic batch item failed custom_id=%s error=%s", custom_id, error_payload)
            if missing:
                logger.error("Anthropic batch missing results for custom_ids=%s", missing)
            raise RuntimeError(
                f"Anthropic batch completed with failures: succeeded={len(results)} errors={len(errors)} missing={len(missing)}"
            )

        logger.info("Anthropic batch complete results=%d", len(results))
        return results
