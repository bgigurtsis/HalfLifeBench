from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List

import tiktoken


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


_jsonl_append_lock = threading.Lock()


def append_jsonl_threadsafe(path: Path, row: Dict[str, Any]) -> None:
    # Multiple ThreadPoolExecutor workers may append concurrently.
    with _jsonl_append_lock:
        append_jsonl(path, row)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def encoding_for_model(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def estimate_text_tokens(text: str, model: str) -> int:
    enc = encoding_for_model(model)
    return len(enc.encode(text))


def estimate_messages_tokens(messages: List[Dict[str, str]], model: str) -> int:
    # Approximate chat token accounting for planning only.
    total = 0
    for msg in messages:
        total += 4
        total += estimate_text_tokens(msg.get("role", ""), model)
        total += estimate_text_tokens(msg.get("content", ""), model)
    total += 2
    return total
