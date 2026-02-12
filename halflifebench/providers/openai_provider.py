from __future__ import annotations

import logging
import threading
import time
from typing import List

from openai import OpenAI

from .base import CompletionResult, Message

logger = logging.getLogger(__name__)


class OpenAIProvider:
    def __init__(self) -> None:
        self.client = OpenAI(timeout=60.0, max_retries=2)
        self._lock = threading.Lock()
        self._use_responses_api_by_model: dict[str, bool] = {}
        self._responses_param_support_by_model: dict[str, dict[str, bool]] = {}
        self._chat_param_support_by_model: dict[str, dict[str, bool]] = {}

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
        normalized_reasoning_effort = (reasoning_effort or "medium").strip().lower()
        if normalized_reasoning_effort not in {"low", "medium", "high"}:
            logger.warning(
                "Invalid reasoning_effort=%r; defaulting to 'medium'",
                reasoning_effort,
            )
            normalized_reasoning_effort = "medium"
        logger.debug(
            "OpenAI complete start model=%s messages=%d max_output_tokens=%d temperature=%.3f seed=%s reasoning_effort=%s",
            model,
            len(messages),
            max_output_tokens,
            temperature,
            seed,
            normalized_reasoning_effort,
        )
        # Prefer Responses API. Fall back to Chat Completions if needed.
        def _supports_param_error(exc: Exception, param_name: str) -> bool:
            text = str(exc).lower()
            return (
                param_name in text
                and (
                    "unsupported value" in text
                    or "unsupported parameter" in text
                    or "is not supported" in text
                    or "does not support" in text
                    or "unexpected keyword argument" in text
                )
            )

        def _responses_call(use_temperature: bool, use_seed: bool, output_token_limit: int):
            kwargs = {
                "model": model,
                "input": messages,
                "max_output_tokens": output_token_limit,
            }
            if use_temperature:
                kwargs["temperature"] = temperature
            if use_seed and seed is not None:
                kwargs["seed"] = seed
            kwargs["reasoning"] = {"effort": normalized_reasoning_effort}
            return self.client.responses.create(**kwargs)

        def _extract_reasoning_tokens(details: object) -> int:
            if details is None:
                return 0
            if isinstance(details, dict):
                value = details.get("reasoning_tokens", 0)
            else:
                value = getattr(details, "reasoning_tokens", 0)
            try:
                return int(value or 0)
            except Exception:
                return 0

        def _responses_incomplete_details(response_obj: object):
            details = getattr(response_obj, "incomplete_details", None)
            if details is None:
                return None
            if isinstance(details, dict):
                reason = details.get("reason")
            else:
                reason = getattr(details, "reason", None)
            return {"reason": reason}

        def _responses_reasoning_tokens(response_obj: object) -> int:
            usage_obj = getattr(response_obj, "usage", None)
            output_details = getattr(usage_obj, "output_tokens_details", None)
            if output_details is None and isinstance(usage_obj, dict):
                output_details = usage_obj.get("output_tokens_details")
            return _extract_reasoning_tokens(output_details)

        def _chat_reasoning_tokens(usage_obj: object) -> int:
            completion_details = getattr(usage_obj, "completion_tokens_details", None)
            if completion_details is None and isinstance(usage_obj, dict):
                completion_details = usage_obj.get("completion_tokens_details")
            return _extract_reasoning_tokens(completion_details)

        def _responses_call_with_empty_retry(use_temperature: bool, use_seed: bool):
            retries = max(0, int(max_empty_retries))
            token_limit = max(1, int(max_output_tokens))
            response_obj = _responses_call(use_temperature, use_seed, token_limit)
            for retry_num in range(1, retries + 1):
                content_text = (getattr(response_obj, "output_text", "") or "").strip()
                status = getattr(response_obj, "status", None)
                if content_text or status != "incomplete":
                    return response_obj
                incomplete = _responses_incomplete_details(response_obj) or {}
                reasoning_tokens = _responses_reasoning_tokens(response_obj)
                usage_obj = getattr(response_obj, "usage", None)
                output_tokens = int(getattr(usage_obj, "output_tokens", 0) or 0)
                logger.info(
                    "OpenAI Responses API retrying empty incomplete output model=%s retry=%d/%d incomplete_reason=%s reasoning_tokens=%d output_tokens=%d max_output_tokens=%d next_max_output_tokens=%d",
                    model,
                    retry_num,
                    retries,
                    incomplete.get("reason"),
                    reasoning_tokens,
                    output_tokens,
                    token_limit,
                    token_limit * 2,
                )
                time.sleep(1.0)
                token_limit *= 2
                response_obj = _responses_call(use_temperature, use_seed, token_limit)
            return response_obj

        with self._lock:
            use_responses_api = self._use_responses_api_by_model.get(model, True)
            resp_support = self._responses_param_support_by_model.setdefault(
                model, {"temperature": True, "seed": True}
            )
            use_resp_temp = resp_support["temperature"]
            use_resp_seed = resp_support["seed"]

        try:
            if not use_responses_api:
                raise RuntimeError("Responses API disabled for this model due to prior incompatibility.")

            try:
                response = _responses_call_with_empty_retry(use_resp_temp, use_resp_seed)
            except Exception as exc:
                if _supports_param_error(exc, "temperature"):
                    with self._lock:
                        was_new = resp_support["temperature"]
                        resp_support["temperature"] = False
                    use_resp_temp = False
                    if was_new:
                        logger.debug(
                            "OpenAI Responses API does not support temperature for model=%s; retrying without temperature",
                            model,
                        )
                    try:
                        response = _responses_call_with_empty_retry(False, use_resp_seed)
                    except Exception as exc2:
                        if _supports_param_error(exc2, "seed"):
                            with self._lock:
                                was_new = resp_support["seed"]
                                resp_support["seed"] = False
                            if was_new:
                                logger.debug(
                                    "OpenAI Responses API does not support seed for model=%s; retrying without seed",
                                    model,
                                )
                            response = _responses_call_with_empty_retry(False, False)
                        else:
                            raise
                elif _supports_param_error(exc, "seed"):
                    with self._lock:
                        was_new = resp_support["seed"]
                        resp_support["seed"] = False
                    use_resp_seed = False
                    if was_new:
                        logger.debug(
                            "OpenAI Responses API does not support seed for model=%s; retrying without seed",
                            model,
                        )
                    try:
                        response = _responses_call_with_empty_retry(use_resp_temp, False)
                    except Exception as exc2:
                        if _supports_param_error(exc2, "temperature"):
                            with self._lock:
                                was_new = resp_support["temperature"]
                                resp_support["temperature"] = False
                            if was_new:
                                logger.debug(
                                    "OpenAI Responses API does not support temperature for model=%s; retrying without temperature",
                                    model,
                                )
                            response = _responses_call_with_empty_retry(False, False)
                        else:
                            raise
                else:
                    raise

            content = response.output_text or ""
            usage = getattr(response, "usage", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            incomplete_details = _responses_incomplete_details(response)
            reasoning_tokens = _responses_reasoning_tokens(response)
            metadata = {
                "id": getattr(response, "id", None),
                "system_fingerprint": getattr(response, "system_fingerprint", None),
                "status": getattr(response, "status", None),
                "incomplete_details": incomplete_details,
                "reasoning_tokens": reasoning_tokens,
                "usage": {
                    "output_tokens_details": {
                        "reasoning_tokens": reasoning_tokens,
                    }
                },
            }
            logger.debug(
                "OpenAI Responses API success model=%s input_tokens=%d output_tokens=%d response_id=%s status=%s incomplete_reason=%s reasoning_tokens=%d",
                model,
                input_tokens,
                output_tokens,
                metadata.get("id"),
                metadata.get("status"),
                (incomplete_details or {}).get("reason"),
                reasoning_tokens,
            )
            return CompletionResult(
                content=content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model,
                metadata=metadata,
            )
        except Exception as responses_exc:
            with self._lock:
                self._use_responses_api_by_model[model] = False
            logger.debug(
                "OpenAI Responses API failed for model=%s; disabling and falling back to Chat Completions. error=%s",
                model,
                responses_exc,
            )
            chat_messages = []
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "developer":
                    role = "system"
                if role not in {"system", "user", "assistant"}:
                    role = "user"
                chat_messages.append({"role": role, "content": content})

            def _chat_call(use_temperature: bool, use_seed: bool):
                kwargs = {
                    "model": model,
                    "messages": chat_messages,
                    "max_completion_tokens": max_output_tokens,
                }
                if use_temperature:
                    kwargs["temperature"] = temperature
                if use_seed and seed is not None:
                    kwargs["seed"] = seed
                return self.client.chat.completions.create(**kwargs)

            with self._lock:
                chat_support = self._chat_param_support_by_model.setdefault(
                    model, {"temperature": True, "seed": True}
                )
                use_chat_temp = chat_support["temperature"]
                use_chat_seed = chat_support["seed"]

            try:
                chat = _chat_call(use_chat_temp, use_chat_seed)
            except Exception as exc:
                if _supports_param_error(exc, "temperature"):
                    with self._lock:
                        was_new = chat_support["temperature"]
                        chat_support["temperature"] = False
                    use_chat_temp = False
                    if was_new:
                        logger.debug(
                            "OpenAI Chat Completions does not support temperature for model=%s; retrying without temperature",
                            model,
                        )
                    try:
                        chat = _chat_call(False, use_chat_seed)
                    except Exception as exc2:
                        if _supports_param_error(exc2, "seed"):
                            with self._lock:
                                was_new = chat_support["seed"]
                                chat_support["seed"] = False
                            if was_new:
                                logger.debug(
                                    "OpenAI Chat Completions does not support seed for model=%s; retrying without seed",
                                    model,
                                )
                            chat = _chat_call(False, False)
                        else:
                            raise
                elif _supports_param_error(exc, "seed"):
                    with self._lock:
                        was_new = chat_support["seed"]
                        chat_support["seed"] = False
                    use_chat_seed = False
                    if was_new:
                        logger.debug(
                            "OpenAI Chat Completions does not support seed for model=%s; retrying without seed",
                            model,
                        )
                    chat = _chat_call(use_chat_temp, False)
                else:
                    raise

            content = chat.choices[0].message.content or ""
            usage = chat.usage
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            reasoning_tokens = _chat_reasoning_tokens(usage)
            metadata = {
                "id": getattr(chat, "id", None),
                "system_fingerprint": getattr(chat, "system_fingerprint", None),
                "finish_reason": getattr(chat.choices[0], "finish_reason", None),
                "reasoning_tokens": reasoning_tokens,
                "usage": {
                    "completion_tokens_details": {
                        "reasoning_tokens": reasoning_tokens,
                    }
                },
            }
            logger.debug(
                "OpenAI Chat Completions success model=%s input_tokens=%d output_tokens=%d response_id=%s finish_reason=%s reasoning_tokens=%d",
                model,
                input_tokens,
                output_tokens,
                metadata.get("id"),
                metadata.get("finish_reason"),
                reasoning_tokens,
            )
            return CompletionResult(
                content=content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model,
                metadata=metadata,
            )
