"""Domain models for the Sales Intelligence & Outreach reference workflow.

These are the shapes that flow between steps as step outputs / inputs. They are
plain Pydantic models — the engine never sees them, it only carries the
``model_dump()`` dicts through ``instance.context`` and step outputs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Tier = Literal["hot", "warm", "cold", "disqualified"]
Channel = Literal["email", "linkedin"]


class Lead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str
    domain: str = ""
    contact_name: str = ""
    contact_title: str = ""
    contact_email: str = ""
    source: str = "unknown"
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_context(cls, ctx: dict[str, Any]) -> Lead:
        """Build a lead from a workflow's trigger context, tolerating a nested
        ``lead`` key or flat fields."""
        data = ctx.get("lead", ctx)
        known = {k: data[k] for k in cls.model_fields if k in data}
        return cls.model_validate(known)


class ResearchDossier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_summary: str = ""
    industry: str = ""
    headcount_estimate: int | None = None
    tech_stack: list[str] = Field(default_factory=list)
    buying_signals: list[str] = Field(default_factory=list)
    recent_news: list[str] = Field(default_factory=list)
    pain_hypotheses: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    already_in_crm: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ICPProfile(BaseModel):
    """The Ideal Customer Profile a lead is scored against. Deployment config."""

    model_config = ConfigDict(extra="forbid")

    name: str = "default"
    target_industries: list[str] = Field(default_factory=list)
    min_headcount: int = 0
    max_headcount: int = 1_000_000
    target_titles: list[str] = Field(default_factory=list)
    required_signals: list[str] = Field(default_factory=list)
    disqualifiers: list[str] = Field(default_factory=list)
    hot_threshold: int = 75
    warm_threshold: int = 50
    qualify_threshold: int = 25


class LeadScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fit_score: int = Field(ge=0, le=100)
    tier: Tier
    rationale: str = ""
    matched_criteria: list[str] = Field(default_factory=list)
    missing_criteria: list[str] = Field(default_factory=list)
    disqualifier_hits: list[str] = Field(default_factory=list)
    recommended_action: str = ""

    @property
    def qualified(self) -> bool:
        return self.tier != "disqualified"


class OutreachDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Channel
    subject: str = ""
    body: str
    call_to_action: str = ""
    personalization_notes: list[str] = Field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.body.split())


class OutreachPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: OutreachDraft
    linkedin: OutreachDraft
    revisions: int = 0
    quality_issues: list[str] = Field(default_factory=list)
    send_ready: bool = True


class DispatchReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skipped: bool = False
    reason: str = ""
    crm_task_ref: str | None = None
    email_ref: str | None = None
