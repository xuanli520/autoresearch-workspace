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


def gpu_state(job: dict[str, Any], **fields: Any) -> None:
    """Forward an official scheduler snapshot; never submit or poll a job here."""
    names = ('request_id', 'job_id', 'state', 'session_id', 'reason', 'scheduler_root',
             'queue', 'sequence', 'projected_start', 'latest_start', 'reconciling')
    snapshot = {key: job[key] for key in names if key in job}
    if 'job_id' not in snapshot and 'id' in job:
        snapshot['job_id'] = job['id']
    if 'sequence' not in snapshot and 'revision' in job:
        snapshot['sequence'] = job['revision']
    emit('gpu.state', **{**snapshot, **fields})


def context_usage(used_tokens: int, *, conversation_id: str, **fields: Any) -> None:
    emit('context.usage', used_tokens=used_tokens, conversation_id=conversation_id, **fields)


def turn_complete(*, credit: bool = True, **fields: Any) -> None:
    emit('turn.completed', credit=credit, **fields)


def turn_credit(*, credited_seconds: float, credit_evidence: str, **fields: Any) -> None:
    """Report auditable research time from a turn that did not close normally.

    The controller still records the turn as failed/stopped and only accepts
    this event when the frozen run explicitly enables partial credit.
    """
    emit('turn.credit', credited_seconds=credited_seconds,
         credit_evidence=credit_evidence, **fields)


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
