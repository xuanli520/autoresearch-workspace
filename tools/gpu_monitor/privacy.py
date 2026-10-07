"""Public monitor records contain operational facts, never scientific evidence."""
import re

_PRIVATE_KEYS = {'b', 'r', 'u', 'tail', 'raw_b64', 'text', '_command', 'cwd',
                 'bindings', 'seeds', 'candidate_manifest', 'protocol_manifest',
                 'signing_key', 'private_roots', 'signature', 'operator_overlay',
                 '_operator_overlay', 'best_in_tail', 'metric_delta_in_tail',
                 'metric_name', 'metric', 'loss', 'accuracy', 'summary_text', 'score',
                 'best', 'latest_score', 'value', 'owner', 'command', 'argv', 'environment',
                 'result_file', 'cgroup_paths', 'cgroup_path', 'container_id', 'container_ids',
                 'frozen_anchors', 'anchors'}
_PRIVATE_PARTS = ('password', 'secret', 'score', 'reward', 'baseline',
                  'reference', 'verifier', 'score_evidence')
_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_PATH = re.compile(r'(?<![\w:])/(?:[^\s,;\]\[{}"\'<>]+)')


def public_record(value, secrets=(), *, key=''):
    """Defense in depth at snapshot and persistence boundaries."""
    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            lowered = str(name).lower()
            if lowered in _PRIVATE_KEYS or any(part in lowered for part in _PRIVATE_PARTS):
                continue
            if lowered.startswith('_') and lowered != '_cache_key':
                continue
            public_name = str(name) if not str(name).startswith('/') else '[path]'
            result[public_name] = public_record(item, secrets, key='config_path' if key == 'config' and lowered == 'path' else lowered)
        return result
    if isinstance(value, (list, tuple)):
        return [public_record(item, secrets, key=key) for item in value]
    if isinstance(value, str):
        text = _ANSI.sub('', value)
        if any(part in text.lower() for part in ('score=', 'score:', 'scientific_score', 'signing_key', 'verifier', 'password', 'reference_solution')):
            return '[redacted]'
        for secret in secrets:
            if secret:
                text = text.replace(secret, '[redacted]')
        # A single registered config path is explicitly part of the public schema.
        if key != 'config_path':
            text = _PATH.sub('[path]', text)
        return text
    return value
