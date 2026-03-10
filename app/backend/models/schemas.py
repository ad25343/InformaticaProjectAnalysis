"""
Pydantic models for InformaticaProjectAnalysis.

All data structures used across the analysis pipeline.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AnalysisStatus(str, enum.Enum):
    PENDING = "PENDING"
    RESOLVING_SOURCE = "RESOLVING_SOURCE"
    PARSING = "PARSING"
    BUILDING_GRAPH = "BUILDING_GRAPH"
    GROUPING = "GROUPING"
    GENERATING_STRATEGY = "GENERATING_STRATEGY"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    APPROVED = "APPROVED"
    DELIVERING = "DELIVERING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class Confidence(str, enum.Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNCLASSIFIED = "UNCLASSIFIED"


class VariationTier(int, enum.Enum):
    TIER_1 = 1  # Parameter only
    TIER_2 = 2  # Minor structural
    TIER_3 = 3  # Fundamental — does not group


class OverrideAction(str, enum.Enum):
    CONFIRM = "confirm"
    MOVE = "move"
    INDIVIDUALIZE = "individualize"


class ReviewDecision(str, enum.Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# ---------------------------------------------------------------------------
# Mapping Parse Models
# ---------------------------------------------------------------------------

class SourceField(BaseModel):
    name: str
    datatype: str
    precision: int = 0
    scale: int = 0
    nullable: bool = True
    key_type: str = ""


class SourceDef(BaseModel):
    name: str
    db_type: str = ""
    dbdname: str = ""
    owner: str = ""
    description: str = ""
    fields: list[SourceField] = Field(default_factory=list)


class TargetField(BaseModel):
    name: str
    datatype: str
    precision: int = 0
    scale: int = 0
    nullable: bool = True
    key_type: str = ""


class TargetDef(BaseModel):
    name: str
    db_type: str = ""
    dbdname: str = ""
    owner: str = ""
    description: str = ""
    fields: list[TargetField] = Field(default_factory=list)


class TransformPort(BaseModel):
    name: str
    datatype: str
    port_type: str = ""  # INPUT, OUTPUT, INPUT/OUTPUT
    expression: str = ""
    precision: int = 0
    scale: int = 0
    default_value: str = ""


class TableAttribute(BaseModel):
    name: str
    value: str


class TransformationDef(BaseModel):
    name: str
    type: str  # "Source Qualifier", "Expression", "Lookup", "Aggregator", etc.
    description: str = ""
    reusable: bool = False
    ports: list[TransformPort] = Field(default_factory=list)
    table_attributes: list[TableAttribute] = Field(default_factory=list)

    @property
    def expressions(self) -> list[dict[str, str]]:
        """Extract port expressions (non-empty)."""
        return [
            {"port": p.name, "expression": p.expression}
            for p in self.ports
            if p.expression
        ]

    @property
    def lookup_table_name(self) -> str | None:
        """Get Lookup Table Name from table attributes if present."""
        for attr in self.table_attributes:
            if attr.name == "Lookup Table Name":
                return attr.value
        return None

    @property
    def sql_query(self) -> str | None:
        """Get SQL override if present."""
        for attr in self.table_attributes:
            if attr.name == "Sql Query" and attr.value:
                return attr.value
        return None

    @property
    def source_table_name(self) -> str | None:
        """Get Source Table Name from table attributes if present."""
        for attr in self.table_attributes:
            if attr.name == "Source Table Name":
                return attr.value
        return None


class Connector(BaseModel):
    from_instance: str
    from_instance_type: str
    from_field: str
    to_instance: str
    to_instance_type: str
    to_field: str


class MappingDef(BaseModel):
    name: str
    description: str = ""
    is_valid: bool = True
    connectors: list[Connector] = Field(default_factory=list)
    target_load_order: list[str] = Field(default_factory=list)


class ParameterDef(BaseModel):
    name: str
    datatype: str = ""
    default_value: str = ""


# ---------------------------------------------------------------------------
# Per-Mapping Parse Result
# ---------------------------------------------------------------------------

class MappingParseResult(BaseModel):
    """Complete parse output for a single mapping XML file."""
    file_path: str
    file_hash: str  # SHA-256
    mapping: MappingDef
    sources: list[SourceDef] = Field(default_factory=list)
    targets: list[TargetDef] = Field(default_factory=list)
    transformations: list[TransformationDef] = Field(default_factory=list)
    parameters: list[ParameterDef] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)

    @property
    def mapping_name(self) -> str:
        return self.mapping.name

    @property
    def transformation_types(self) -> list[str]:
        """Ordered list of transformation types."""
        return [t.type for t in self.transformations]

    @property
    def source_table_names(self) -> list[str]:
        return [s.name for s in self.sources]

    @property
    def target_table_names(self) -> list[str]:
        return [t.name for t in self.targets]

    @property
    def lookup_table_names(self) -> list[str]:
        """Tables referenced via Lookup transformations."""
        names = []
        for t in self.transformations:
            if t.lookup_table_name:
                names.append(t.lookup_table_name)
        return names


# ---------------------------------------------------------------------------
# Project Graph Models
# ---------------------------------------------------------------------------

class DependencyEdge(BaseModel):
    from_mapping: str  # Mapping that writes the table
    to_mapping: str  # Mapping that looks up the table
    via_table: str  # The shared table name


class SharedAsset(BaseModel):
    table_name: str
    reference_type: str = "lookup"
    referenced_by: list[str] = Field(default_factory=list)


class ProjectGraph(BaseModel):
    """Aggregated project-level graph across all mappings."""
    mapping_count: int = 0
    all_sources: list[SourceDef] = Field(default_factory=list)
    all_targets: list[TargetDef] = Field(default_factory=list)
    dependency_edges: list[DependencyEdge] = Field(default_factory=list)
    shared_assets: list[SharedAsset] = Field(default_factory=list)
    target_to_mapping: dict[str, str] = Field(default_factory=dict)
    # Mapping name → list of lookup table names
    mapping_lookups: dict[str, list[str]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Spine / Fingerprint Models
# ---------------------------------------------------------------------------

class SpineStep(BaseModel):
    """One step in a mapping's transformation spine."""
    instance_name: str
    instance_type: str  # "Source Qualifier", "Expression", "Target Definition", etc.


class MappingSpine(BaseModel):
    """Canonical spine extracted from a mapping's connectors."""
    mapping_name: str
    steps: list[SpineStep] = Field(default_factory=list)
    spine_signature: str = ""  # e.g. "SQ → EXP → TARGET"

    @property
    def step_types(self) -> list[str]:
        return [s.instance_type for s in self.steps]


# ---------------------------------------------------------------------------
# Pattern Group Models
# ---------------------------------------------------------------------------

class GroupMember(BaseModel):
    mapping_name: str
    confidence: Confidence = Confidence.UNCLASSIFIED
    variation_tier: VariationTier = VariationTier.TIER_1
    variation_notes: str | None = None
    override: str | None = None


class PatternGroup(BaseModel):
    group_id: str
    group_name: str
    spine_signature: str
    members: list[GroupMember] = Field(default_factory=list)
    externalized_params: list[str] = Field(default_factory=list)
    template_hints: str = ""

    @property
    def member_count(self) -> int:
        return len(self.members)


# ---------------------------------------------------------------------------
# Strategy Models
# ---------------------------------------------------------------------------

class UniqueMapping(BaseModel):
    mapping_name: str
    reason: str
    risk_flags: list[str] = Field(default_factory=list)


class ExecutionStage(BaseModel):
    stage: int
    mappings: list[str]


class StrategySummary(BaseModel):
    total_mappings: int
    pattern_groups: int
    template_candidates: int
    unique_mappings: int
    scope_reduction_pct: float


class StrategyJSON(BaseModel):
    strategy_version: int = 1
    project_name: str
    analysis_job_id: str
    analyzed_at: str

    summary: StrategySummary
    pattern_groups: list[PatternGroup] = Field(default_factory=list)
    unique_mappings: list[UniqueMapping] = Field(default_factory=list)
    shared_assets: list[SharedAsset] = Field(default_factory=list)
    dependency_dag: list[DependencyEdge] = Field(default_factory=list)
    execution_order: list[list[str]] = Field(default_factory=list)

    review: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Review / Override Models
# ---------------------------------------------------------------------------

class MappingOverride(BaseModel):
    mapping_name: str
    action: OverrideAction
    from_group: str | None = None
    to_group: str | None = None
    notes: str = ""


class ReviewRecord(BaseModel):
    reviewer_name: str
    reviewer_role: str = ""
    review_date: str = ""
    decision: ReviewDecision
    overrides: list[MappingOverride] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Project Config Models
# ---------------------------------------------------------------------------

class ProjectSource(BaseModel):
    type: str  # folder, repo, zip, s3
    location: str
    branch: str | None = None
    path: str | None = None


class ScopeGlobs(BaseModel):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class ProjectScope(BaseModel):
    mappings: ScopeGlobs = Field(default_factory=ScopeGlobs)
    workflows: ScopeGlobs = Field(default_factory=ScopeGlobs)
    parameters: ScopeGlobs = Field(default_factory=ScopeGlobs)
    default_parameter_env: str = "dev"


class AnalysisSettings(BaseModel):
    fingerprint_strictness: str = "moderate"
    min_group_size: int = 2
    confidence_threshold: float = 0.7
    detect_shared_assets: bool = True
    build_dependency_dag: bool = True
    classify_expressions: bool = True


class Reviewer(BaseModel):
    name: str = ""
    email: str = ""


class ReviewConfig(BaseModel):
    tech_lead: Reviewer = Field(default_factory=Reviewer)
    leadership: Reviewer = Field(default_factory=Reviewer)
    auto_notify: bool = False


class OutputConfig(BaseModel):
    output_dir: str = "./output"
    strategy_format: str = "json"


class NotificationEvents(BaseModel):
    on_analysis_complete: bool = True
    on_strategy_ready: bool = True
    on_review_approved: bool = True


class NotificationConfig(BaseModel):
    webhook_url: str = ""
    events: NotificationEvents = Field(default_factory=NotificationEvents)


class ProjectConfig(BaseModel):
    """Parsed and validated *.project.yaml configuration."""

    class ProjectMeta(BaseModel):
        name: str
        version: str = "1.0"
        owner: str = ""
        description: str = ""

    project: ProjectMeta
    source: ProjectSource
    scope: ProjectScope = Field(default_factory=ProjectScope)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)


# ---------------------------------------------------------------------------
# Analysis Job Model
# ---------------------------------------------------------------------------

class AnalysisJob(BaseModel):
    job_id: str
    project_name: str
    project_config: ProjectConfig
    status: AnalysisStatus = AnalysisStatus.PENDING
    current_phase: int = 0
    created_at: str = ""
    updated_at: str = ""
    error: str | None = None

    # Per-phase artifacts stored as the pipeline runs
    resolved_files: list[str] = Field(default_factory=list)
    parse_results: list[MappingParseResult] = Field(default_factory=list)
    project_graph: ProjectGraph | None = None
    spines: list[MappingSpine] = Field(default_factory=list)
    pattern_groups: list[PatternGroup] = Field(default_factory=list)
    unique_mappings: list[UniqueMapping] = Field(default_factory=list)
    strategy: StrategyJSON | None = None
    review: ReviewRecord | None = None
