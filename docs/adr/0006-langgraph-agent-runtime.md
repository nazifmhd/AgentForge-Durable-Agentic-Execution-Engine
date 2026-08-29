# ADR-0006: LangGraph for the agent runtime

- **Status:** accepted
- **Date:** 2026-08-29

## Context

Individual steps are handled by agents that themselves have internal structure (gather →
synthesize → validate → maybe retry). We need graph structure, conditional edges, and a
place to hang tool calls without hand-rolling it.

## Decision

Agents are LangGraph `StateGraph`s. AgentForge owns durability at the **step boundary**;
LangGraph's own checkpointer is **disabled** — an agent's internal graph runs to completion
within one step attempt and is transactional at that grain. If an agent needs long internal
pauses (rare), it is modeled as multiple AgentForge steps instead.

## Consequences

- One clear persistence owner; no double-writing agent sub-state.
- LangGraph is a pinned dependency (`>=0.2.60,<0.3` initially); its churn is contained to
  `agents/` and `use_cases/`, never `core/`.
- Agent code stays declarative and readable.

## Alternatives considered

- **Raw provider tool-use loop** — we'd rebuild graph/edge/state plumbing.
- **LangGraph with its Postgres checkpointer as the substrate** — couples our durability
  model to LangGraph internals and its schema; rejected.
