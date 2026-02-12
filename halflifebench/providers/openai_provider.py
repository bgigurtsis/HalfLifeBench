from __future__ import annotations

import json
import logging
import threading
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from openai import OpenAI

from .base import BatchRequest, CompletionResult, Message

logger = logging.getLogger(__name__)


class OpenAIProvider:
    def __init__(self) -> None:
        self.client = OpenAI(timeout=60.0, max_retries=2)
        self._lock = threading.Lock()
        self._use_responses_api_by_model: dict[str, bool] = {}
        self._responses_param_support_by_model: dict[str, dict[str, bool]] = {}
        self._chat_param_support_by_model: dict[str, dict[str, bool]] = {}

    @staticmethod
    def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def _to_chat_messages(messages: List[Message]) -> List[dict[str, str]]:
        chat_messages: List[dict[str, str]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant"}:
                role = "user"
            chat_messages.append({"role": role, "content": content})
        return chat_messages

    @staticmethod
    def _normalize_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text_parts.append(str(item.get("text", "")))
                    else:
                        text_parts.append(str(item))
                else:
                    text_parts.append(str(item))
            return "".join(text_parts)
        if content is None:
            return ""
        return str(content)

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
        with tempfile.TemporaryDirectory(prefix="hlb_openai_batch_") as temp_dir:
            batch_path = Path(temp_dir) / "requests.jsonl"
            with batch_path.open("w", encoding="utf-8") as handle:
                for req in requests:
                    custom_id = str(req.get("custom_id", "")).strip()
                    model = str(req.get("model", "")).strip()
                    messages = req.get("messages")
                    if not custom_id:
                        raise ValueError("OpenAI batch request missing custom_id")
                    if custom_id in expected_ids:
                        raise ValueError(f"Duplicate OpenAI batch custom_id: {custom_id}")
                    if not model:
                        raise ValueError(f"OpenAI batch request {custom_id} missing model")
                    if not isinstance(messages, list):
                        raise ValueError(f"OpenAI batch request {custom_id} missing messages list")
                    expected_ids.add(custom_id)
                    expected_models[custom_id] = model

                    body: Dict[str, Any] = {
                        "model": model,
                        "messages": self._to_chat_messages(messages),
                        "max_tokens": int(req.get("max_output_tokens", 700)),
                    }
                    if "temperature" in req and req.get("temperature") is not None:
                        body["temperature"] = float(req.get("temperature"))
                    seed = req.get("seed")
                    if seed is not None:
                        body["seed"] = int(seed)

                    handle.write(
                        json.dumps(
                            {
                                "custom_id": custom_id,
                                "method": "POST",
                                "url": "/v1/chat/completions",
                                "body": body,
                            }
                        )
                        + "\n"
                    )

            with batch_path.open("rb") as file_handle:
                uploaded = self.client.files.create(file=file_handle, purpose="batch")
            input_file_id = self._obj_get(uploaded, "id")
            logger.info("OpenAI batch input uploaded file_id=%s requests=%d", input_file_id, len(requests))

            batch = self.client.batches.create(
                input_file_id=input_file_id,
                endpoint="/v1/chat/completions",
                completion_window="24h",
            )
            batch_id = self._obj_get(batch, "id")
            logger.info("OpenAI batch submitted batch_id=%s", batch_id)

            terminal_failure = {"failed", "expired", "cancelled", "canceled"}
            while True:
                batch = self.client.batches.retrieve(batch_id)
                status = str(self._obj_get(batch, "status", "unknown"))
                request_counts = self._obj_get(batch, "request_counts", {})
                logger.info(
                    "OpenAI batch poll batch_id=%s status=%s counts=%s",
                    batch_id,
                    status,
                    request_counts,
                )
                if status == "completed":
                    break
                if status in terminal_failure:
                    raise RuntimeError(f"OpenAI batch {batch_id} ended with status={status}")
                time.sleep(safe_poll_interval)

            output_file_id = self._obj_get(batch, "output_file_id")
            if not output_file_id:
                raise RuntimeError(f"OpenAI batch {batch_id} completed without output_file_id")

            content_response = self.client.files.content(output_file_id)
            content_payload = self._obj_get(content_response, "content", content_response)
            if isinstance(content_payload, (bytes, bytearray)):
                lines = content_payload.decode("utf-8").splitlines()
            else:
                lines = str(content_payload).splitlines()

        results: Dict[str, CompletionResult] = {}
        errors: Dict[str, Any] = {}
        for line in lines:
            if not line.strip():
                continue
            row = json.loads(line)
            custom_id = str(row.get("custom_id", "")).strip()
            if not custom_id:
                continue

            response_obj = row.get("response")
            error_obj = row.get("error")
            if error_obj is not None:
                errors[custom_id] = error_obj
                continue
            if not isinstance(response_obj, dict):
                errors[custom_id] = {"error": "missing response object", "row": row}
                continue

            status_code = int(response_obj.get("status_code", 0) or 0)
            body = response_obj.get("body", {})
            if status_code and status_code >= 400:
                errors[custom_id] = {"status_code": status_code, "body": body}
                continue
            if not isinstance(body, dict):
                errors[custom_id] = {"error": "missing response body", "status_code": status_code}
                continue

            choices = body.get("choices", [])
            if not choices:
                errors[custom_id] = {"error": "missing choices", "status_code": status_code, "body": body}
                continue
            first_choice = choices[0]
            message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
            content = self._normalize_content(message.get("content"))
            usage = body.get("usage", {}) if isinstance(body.get("usage"), dict) else {}
            input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
            output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
            model_name = str(body.get("model") or "")
            metadata = {
                "id": body.get("id"),
                "batch_status_code": status_code,
                "finish_reason": first_choice.get("finish_reason") if isinstance(first_choice, dict) else None,
                "request_id": response_obj.get("request_id"),
                "usage": usage,
            }
            results[custom_id] = CompletionResult(
                content=content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model_name or expected_models.get(custom_id, ""),
                metadata=metadata,
            )

        missing = sorted(expected_ids - set(results.keys()))
        if missing or errors:
            if errors:
                for custom_id, error_payload in errors.items():
                    logger.error("OpenAI batch item failed custom_id=%s error=%s", custom_id, error_payload)
            if missing:
                logger.error("OpenAI batch missing results for custom_ids=%s", missing)
            raise RuntimeError(
                f"OpenAI batch completed with failures: succeeded={len(results)} errors={len(errors)} missing={len(missing)}"
            )

        logger.info("OpenAI batch complete results=%d", len(results))
        return results

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
