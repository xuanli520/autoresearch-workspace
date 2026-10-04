"""Controller-side completion API; do not install in public candidate workspaces."""
from tools.research_handoff.core.completion import (
    artifact, attest_docker_isolation, contract_for_run, digest, issue_receipt,
    publish_reward, register_jobs, validate_contract, validate_receipt, write_score_result,
)

__all__ = [
    'artifact', 'attest_docker_isolation', 'contract_for_run', 'digest', 'issue_receipt',
    'publish_reward', 'register_jobs', 'validate_contract', 'validate_receipt', 'write_score_result',
]
