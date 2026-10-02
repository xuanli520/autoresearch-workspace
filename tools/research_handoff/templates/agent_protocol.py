"""Provider-independent helpers. Call from the agent's actual progress loop."""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Any


def context() -> dict:
    return json.loads(Path(os.environ['AUTORESEARCH_CONTEXT_FILE']).read_text())


def emit(kind: str, **fields: Any) -> None:
    generation = int(os.environ['AUTORESEARCH_CONTEXT_GENERATION'])
    payload = {**fields, 'autoresearch': kind, 'generation': generation, 'at_epoch': time.time()}
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False), flush=True)


def heartbeat(**fields: Any) -> None:
    emit('heartbeat', **fields)


def context_usage(used_tokens: int, *, conversation_id: str, **fields: Any) -> None:
    emit('context.usage', used_tokens=used_tokens, conversation_id=conversation_id, **fields)


def turn_complete(*, credit: bool = True, **fields: Any) -> None:
    emit('turn.completed', credit=credit, **fields)


def compact(summary: str) -> None:
    """Persist the summary through the controller, then exit the current turn."""
    emit('context.compact', summary=summary)


def compaction_requested() -> bool:
    return Path(os.environ['AUTORESEARCH_TURN_DIR'], 'context-request.json').exists()


def fits_request(used_tokens: int, next_input_tokens: int, max_output_tokens: int) -> bool:
    """Check before invoking a provider; count its full prompt/tools/attachments.

    This does not tokenize text. Supply the actual provider/tokenizer counts.
    Keep room for a summary and report usage after every provider response.
    """
    cfg = context()
    values = (used_tokens, next_input_tokens, max_output_tokens)
    if any(type(v) is not int or v < 0 for v in values):
        raise ValueError('token counts must be nonnegative integers')
    # Keep the request below the controller's compaction line and fail closed
    # if a previous report already crossed the provider's hard capacity.
    maximum = int(cfg['max_tokens'])
    compact_at = int(cfg['compact_at_tokens'])
    if used_tokens > maximum or next_input_tokens + max_output_tokens > maximum:
        return False
    return sum(values) <= compact_at and not compaction_requested()
