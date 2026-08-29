# Lessons Learned

Real problems hit during the build and how they were solved. Added as they happen.

## Phase 0

- **The blueprint's `checkpoint_version` optimistic lock doesn't survive parallelism.**
  A single version integer on the instance row means every parallel step completion
  contends on it. Switched the core to event sourcing (ADR-0002) before writing any
  execution code — cheaper to decide now than to migrate later.
- **Model registry rots.** The blueprint's hardcoded `claude-sonnet-4-6` / `gpt-4o` prices
  were already stale. Moved to `config/models.yaml`, hot-reloadable, verified current IDs
  and pricing against the provider docs.
