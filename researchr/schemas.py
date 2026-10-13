"""Data models shared by the pipeline, report renderer and web app."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

FACT_CATEGORIES = [
    "career", "education", "publication", "talk_media", "code", "writing_social",
    "award", "organization", "hobby_interest", "opinion", "news", "company_context", "other",
]


class Target(BaseModel):
    name: str
    hints: str = ""
    goal: str = ""
    context: str = ""
    me: str = ""             # visitor's own "about you" text for common ground (public mode)
    use_owner_profile: bool = False  # owner runs use profile/me.json


class Identity(BaseModel):
    canonical_name: str
    current_role: str = ""
    org: str = ""
    location: str = ""
    disambiguators: list[str] = Field(default_factory=list)
    known_urls: list[str] = Field(default_factory=list)
    ambiguous: bool = False
    ambiguity_note: str = ""
    confidence: float = 0.5


class Source(BaseModel):
    id: str
    url: str
    title: str = ""
    snippet: str = ""
    domain: str = ""
    kind: str = "unknown"  # self | institutional | news | aggregator | social | snippet | unknown
    p_same_person: float = 0.5
    value: float = 0.5
    fetched: bool = False
    fetch_method: str = ""
    text: str = Field(default="", exclude=True)


class RawFact(BaseModel):
    statement: str
    category: str = "other"
    evidence_quote: str = ""
    date: str = ""
    source_id: str = ""
    quote_verified: bool = False


class Fact(BaseModel):
    id: str
    statement: str
    category: str = "other"
    date: str = ""
    source_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class Inference(BaseModel):
    id: str
    statement: str
    category: str = "other"
    probability: float = 0.5
    reasoning: str = ""
    based_on: list[str] = Field(default_factory=list)
    verify_by: str = ""


class Overlap(BaseModel):
    type: str
    mine: str
    theirs: str
    strength: float = 0.5
    description: str = ""


class InsightPoint(BaseModel):
    text: str
    facts: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class Insight(BaseModel):
    kind: str = "work"
    title: str
    summary: str = ""
    points: list[InsightPoint] = Field(default_factory=list)
    so_what: str = ""
    confidence: float = 0.5
    source_ids: list[str] = Field(default_factory=list)


class Theme(BaseModel):
    theme: str
    why: str = ""
    angles: list[str] = Field(default_factory=list)
    their_depth: str = ""
    priority: int = 2  # 1 = strongest


class GoalStrategy(BaseModel):
    their_incentives: list[str] = Field(default_factory=list)
    framing: str = ""
    objections: list[dict] = Field(default_factory=list)  # {objection, response}
    ideal_outcome: str = ""
    next_step: str = ""


class Report(BaseModel):
    target: Target
    identity: Identity
    summary: str = ""
    glance: list[str] = Field(default_factory=list)
    insights: list[Insight] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    inferences: list[Inference] = Field(default_factory=list)
    overlaps: list[Overlap] = Field(default_factory=list)
    themes: list[Theme] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)
    strategy: Optional[GoalStrategy] = None
    footprint: dict = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
