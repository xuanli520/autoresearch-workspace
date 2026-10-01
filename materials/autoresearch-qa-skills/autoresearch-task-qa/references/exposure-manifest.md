# Exposure manifest

Place `qa_exposure_manifest.json` at the workspace root when the actual Agent
view is narrower than the authoring bundle. Example:

```json
{
  "schema_version": 1,
  "agent_visible_paths": [
    "INIT_PROMPT.md",
    "harbor_task/instruction.md",
    "harbor_task/task.toml",
    "harbor_task/environment/starter",
    "harbor_task/environment/public_assets",
    "harbor_task/solution"
  ],
  "agent_writable_paths": ["harbor_task/solution"],
  "trusted_only_paths": ["harbor_task/tests", "expert_evidence"],
  "hidden_injection_paths": ["/trusted-input/hidden"],
  "final_image_digest": "sha256:...",
  "generated_agent_view_manifest": "evidence/agent-view-files.sha256",
  "verified_as_uid": 1000
}
```

Paths inside the bundle are workspace-relative; runtime-only paths are absolute.
The checker currently uses `agent_visible_paths` to scope static leak detection.
All other fields guide manual/dynamic checks. A trusted runner must regenerate a
recursive path/type/mode/owner/hash list from the final image plus mounts and
compare it with `generated_agent_view_manifest`. Reject extra files, symlinks,
special files, unexpected writable ancestors, and digest mismatches.
