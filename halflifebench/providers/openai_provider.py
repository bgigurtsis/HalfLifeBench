from __future__ import annotations

import logging
from typing import List

from openai import OpenAI

from .base import CompletionResult, Message

logger = logging.getLogger(__name__)


class OpenAIProvider:
    def __init__(self) -> None:
        self.client = OpenAI(timeout=60.0, max_retries=2)
        self._use_responses_api_by_model: dict[str, bool] = {}
        self._chat_param_support_by_model: dict[str, dict[str, bool]] = {}

    def complete(
        self,
        *,
        model: str,
        messages: List[Message],
        temperature: float = 0.0,
        seed: int | None = None,
        max_output_tokens: int = 700,
    ) -> CompletionResult:
        logger.debug(
            "OpenAI complete start model=%s messages=%d max_output_tokens=%d temperature=%.3f seed=%s",
            model,
            len(messages),
            max_output_tokens,
            temperature,
            seed,
        )
        # Prefer Responses API. Fall back to Chat Completions if needed.
        def _supports_param_error(exc: Exception, param_name: str) -> bool:
            text = str(exc).lower()
            return (
                param_name in text
                and (
                    "unsupported value" in text
                    or "does not support" in text
                    or "unexpected keyword argument" in text
                )
            )

        def _responses_call(use_temperature: bool, use_seed: bool):
            kwargs = {
                "model": model,
                "input": messages,
                "max_output_tokens": max_output_tokens,
            }
            if use_temperature:
                kwargs["temperature"] = temperature
            if use_seed and seed is not None:
                kwargs["seed"] = seed
            return self.client.responses.create(**kwargs)

        use_responses_api = self._use_responses_api_by_model.get(model, True)
        try:
            if not use_responses_api:
                raise RuntimeError("Responses API disabled for this model due prior incompatibility.")

            try:
                response = _responses_call(True, True)
            except Exception as exc:
                if _supports_param_error(exc, "temperature"):
                    logger.warning(
                        "OpenAI Responses API does not support temperature for model=%s; retrying without temperature",
                        model,
                    )
                    try:
                        response = _responses_call(False, True)
                    except Exception as exc2:
                        if _supports_param_error(exc2, "seed"):
                            logger.warning(
                                "OpenAI Responses API does not support seed for model=%s; retrying without seed",
                                model,
                            )
                            response = _responses_call(False, False)
                        else:
                            raise
                elif _supports_param_error(exc, "seed"):
                    logger.warning(
                        "OpenAI Responses API does not support seed for model=%s; retrying without seed",
                        model,
                    )
                    response = _responses_call(True, False)
                else:
                    raise

            content = response.output_text or ""
            usage = getattr(response, "usage", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            metadata = {
                "id": getattr(response, "id", None),
                "system_fingerprint": getattr(response, "system_fingerprint", None),
                "status": getattr(response, "status", None),
            }
            logger.debug(
                "OpenAI Responses API success model=%s input_tokens=%d output_tokens=%d response_id=%s",
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
        except Exception as responses_exc:
            if "unexpected keyword argument 'seed'" in str(responses_exc).lower():
                self._use_responses_api_by_model[model] = False
                logger.warning(
                    "Disabling OpenAI Responses API for model=%s due SDK/API incompatibility with seed.",
                    model,
                )
            logger.warning(
                "OpenAI Responses API failed for model=%s; falling back to Chat Completions. error=%s",
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

            chat_support = self._chat_param_support_by_model.setdefault(
                model, {"temperature": True, "seed": True}
            )

            try:
                chat = _chat_call(chat_support["temperature"], chat_support["seed"])
            except Exception as exc:
                if _supports_param_error(exc, "temperature"):
                    chat_support["temperature"] = False
                    logger.warning(
                        "OpenAI Chat Completions does not support temperature for model=%s; retrying without temperature",
                        model,
                    )
                    try:
                        chat = _chat_call(False, chat_support["seed"])
                    except Exception as exc2:
                        if _supports_param_error(exc2, "seed"):
                            chat_support["seed"] = False
                            logger.warning(
                                "OpenAI Chat Completions does not support seed for model=%s; retrying without seed",
                                model,
                            )
                            chat = _chat_call(False, False)
                        else:
                            raise
                elif _supports_param_error(exc, "seed"):
                    chat_support["seed"] = False
                    logger.warning(
                        "OpenAI Chat Completions does not support seed for model=%s; retrying without seed",
                        model,
                    )
                    chat = _chat_call(chat_support["temperature"], False)
                else:
                    raise

            content = chat.choices[0].message.content or ""
            usage = chat.usage
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            metadata = {
                "id": getattr(chat, "id", None),
                "system_fingerprint": getattr(chat, "system_fingerprint", None),
                "finish_reason": getattr(chat.choices[0], "finish_reason", None),
            }
            logger.debug(
                "OpenAI Chat Completions success model=%s input_tokens=%d output_tokens=%d response_id=%s",
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
