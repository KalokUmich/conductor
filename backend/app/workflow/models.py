"""Pydantic models for config-driven workflow engine.

Validates:
  - Workflow YAML (pr_review.yaml, code_explorer.yaml)
  - Agent .md frontmatter (YAML between --- markers)
  - Classifier configuration
  - Budget defaults and size multipliers
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Agent config (parsed from .md frontmatter)
# ---------------------------------------------------------------------------


class ToolsConfig(BaseModel):
    """Tool configuration for an agent."""
    core: bool = True                         # include workflow's core_tools
    extra: List[str] = Field(default_factory=list)  # additional tools


class TriggerConfig(BaseModel):
    """When this agent should be dispatched."""
    risk_dimensions: List[str] = Field(default_factory=list)
    always: bool = False


class AgentLimits(BaseModel):
    """Resource limits for an agent (Brain architecture)."""
    max_iterations: int = 20
    budget_tokens: int = 300_000
    evidence_retries: int = 2
    temperature: Optional[float] = None  # None = provider default; 0.0-1.0


class QualityConfig(BaseModel):
    """Quality check settings for an agent — drives evidence check and Brain review."""
    evidence_check: bool = True           # run rule-based evidence check
    min_file_refs: int = 1                # minimum file:line references in answer
    min_tool_calls: int = 2              # minimum tool calls made
    need_brain_review: bool = False       # Brain evaluates findings before synthesis


class AgentConfig(BaseModel):
    """A single agent definition, parsed from a .md file with YAML frontmatter.

    Supports both legacy format (type/model_role/budget_weight/tools.core+extra)
    and new Brain format (model/limits/tools as flat list/description).
    """
    name: str
    type: Literal["explorer", "judge"] = "explorer"
    category: Optional[str] = None

    # --- New Brain format fields ---
    description: str = ""                     # Brain reads this to match queries to agents
    model: Literal["explorer", "strong"] = "explorer"
    strategy: str = ""                        # Layer 2 strategy key (e.g., "code_review")
    skill: str = ""                            # Layer 3 investigation skill (e.g., "business_flow")
    focus: str = ""                            # Focus directive prepended to query in swarm dispatch
    limits: AgentLimits = Field(default_factory=AgentLimits)
    quality: QualityConfig = Field(default_factory=QualityConfig)

    # --- Legacy format fields (kept for backward compat) ---
    model_role: Literal["explorer", "strong"] = "explorer"

    # Tools — new format: flat list; legacy: ToolsConfig with core+extra
    tools: Any = Field(default_factory=ToolsConfig)

    # Budget (legacy)
    budget_weight: float = 1.0
    max_tokens: Optional[int] = None          # judge agents only

    # Dispatch trigger (used by parallel_all_matching mode)
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)

    # File scope for PR review agents
    file_scope: List[str] = Field(default_factory=lambda: ["business_logic"])

    # Data flow declarations
    input: List[str] = Field(default_factory=list)
    output: Optional[str] = None

    # Agent instructions (Markdown body from .md file)
    instructions: str = ""

    # Source file path (set by loader)
    source_path: Optional[str] = None

    @model_validator(mode="after")
    def _sync_model_fields(self) -> "AgentConfig":
        """Keep model and model_role in sync."""
        # If new 'model' field was explicitly set, sync to model_role
        if self.model != "explorer" or self.model_role == "explorer":
            self.model_role = self.model
        elif self.model_role != "explorer":
            self.model = self.model_role
        return self

    @property
    def tool_list(self) -> List[str]:
        """Get flat tool list regardless of format."""
        if isinstance(self.tools, list):
            return self.tools
        if isinstance(self.tools, ToolsConfig):
            return self.tools.extra
        return []


# ---------------------------------------------------------------------------
# Brain orchestrator config (loaded from brain.yaml)
# ---------------------------------------------------------------------------


class BrainLimits(BaseModel):
    """Resource limits for the Brain orchestrator."""
    max_iterations: int = 20
    budget_tokens: int = 100_000              # Brain's own budget
    total_session_tokens: int = 800_000       # total across Brain + sub-agents
    max_concurrent_agents: int = 3
    sub_agent_timeout: float = 300.0          # 5 minutes per sub-agent
    max_depth: int = 2                        # Brain(0) → agent(1) → sub-agent(2)


class BrainConfig(BaseModel):
    """Brain orchestrator configuration, loaded from brain.yaml."""
    model: str = "strong"
    limits: BrainLimits = Field(default_factory=BrainLimits)
    core_tools: List[str] = Field(default_factory=lambda: [
        "grep", "read_file", "find_symbol", "file_outline",
        "compressed_view", "expand_symbol",
    ])


# ---------------------------------------------------------------------------
# PR Brain config (loaded from brains/pr_review.yaml)
# ---------------------------------------------------------------------------


class PostProcessingConfig(BaseModel):
    """Post-processing settings for PR Brain."""
    min_confidence: float = 0.6
    max_findings: int = 10


class PRBrainConfig(BaseModel):
    """PR Brain orchestrator configuration, loaded from brains/pr_review.yaml."""
    name: str = "pr_review"
    description: str = ""
    model: str = "strong"
    limits: BrainLimits = Field(default_factory=BrainLimits)
    review_agents: List[str] = Field(default_factory=lambda: [
        "correctness", "concurrency", "security", "reliability", "test_coverage",
    ])
    arbitrator: str = "pr_arbitrator"
    budget_weights: Dict[str, float] = Field(default_factory=lambda: {
        "correctness": 1.00,
        "concurrency": 0.85,
        "security": 0.75,
        "reliability": 0.70,
        "test_coverage": 0.55,
    })
    post_processing: PostProcessingConfig = Field(default_factory=PostProcessingConfig)


# ---------------------------------------------------------------------------
# Swarm preset config (loaded from swarms/*.yaml)
# ---------------------------------------------------------------------------


class SwarmConfig(BaseModel):
    """A swarm preset — a named group of agents to run together."""
    name: str
    description: str = ""
    mode: Literal["parallel", "sequential"] = "parallel"
    agents: List[str] = Field(default_factory=list)  # agent names
    synthesis_guide: str = ""  # synthesis instructions for Brain


# ---------------------------------------------------------------------------
# Classifier config
# ---------------------------------------------------------------------------


class ThresholdConfig(BaseModel):
    """Risk level thresholds for risk_pattern classifier."""
    count: int = 0
    ratio: float = 0.0


class ThresholdsConfig(BaseModel):
    """Thresholds mapping for risk_pattern classifier."""
    high: ThresholdConfig = Field(default_factory=lambda: ThresholdConfig(count=5, ratio=0.3))
    medium: ThresholdConfig = Field(default_factory=lambda: ThresholdConfig(count=2, ratio=0.15))


class ClassifierConfig(BaseModel):
    """Classifier configuration within a workflow."""
    type: Literal["risk_pattern", "keyword_pattern"]
    thresholds: Optional[ThresholdsConfig] = None  # risk_pattern only


class DispatchConfig(BaseModel):
    """Dispatch strategy configuration."""
    mode: Literal["classifier", "llm", "hybrid"] = "classifier"
    classifier: ClassifierConfig


# ---------------------------------------------------------------------------
# Route config
# ---------------------------------------------------------------------------


class BoostRule(BaseModel):
    """Conditional boost for risk_pattern classifier."""
    when: str                                 # e.g. "schema_files > 0"
    min_level: str = "medium"                 # minimum risk level to set


class StageConfig(BaseModel):
    """A single pipeline stage."""
    stage: str                                # stage name (e.g. "explore", "arbitrate")
    parallel: bool = False                    # run agents in this stage concurrently
    agents: List[str] = Field(default_factory=list)  # paths to agent .md files


class RouteConfig(BaseModel):
    """A classifier route — maps a dimension/query type to a pipeline."""
    # Patterns for matching (one of these depending on classifier type)
    text_patterns: List[str] = Field(default_factory=list)   # keyword_pattern
    file_patterns: List[str] = Field(default_factory=list)   # risk_pattern
    boost_rules: List[BoostRule] = Field(default_factory=list)
    # Example queries for LLM-based classification (3-5 per route)
    examples: List[str] = Field(default_factory=list)

    # Pipeline stages for this route
    pipeline: List[StageConfig] = Field(default_factory=list)

    # Delegate to another workflow instead of running pipeline
    delegate: Optional[str] = None

    @model_validator(mode="after")
    def _validate_pipeline_or_delegate(self) -> "RouteConfig":
        if self.delegate and self.pipeline:
            raise ValueError("Route cannot have both 'delegate' and 'pipeline'")
        if not self.delegate and not self.pipeline:
            raise ValueError("Route must have either 'pipeline' or 'delegate'")
        return self


# ---------------------------------------------------------------------------
# Budget config
# ---------------------------------------------------------------------------


class SizeMultiplierEntry(BaseModel):
    """Budget multiplier for a PR size range."""
    max_lines: int
    factor: float


class BudgetDefaults(BaseModel):
    """Global budget defaults for a workflow."""
    base_tokens: int = 550_000
    base_iterations: int = 25
    sub_fraction: float = 0.7
    min_iterations: int = 8
    size_multiplier: Optional[Dict[str, SizeMultiplierEntry]] = None
    reject_above: Optional[int] = None        # max lines before rejecting PR


# ---------------------------------------------------------------------------
# Post-processing config
# ---------------------------------------------------------------------------


class EvidenceGateConfig(BaseModel):
    """Evidence quality gate for critical findings."""
    critical_min_evidence: int = 2
    critical_require_file: bool = True
    critical_require_line: bool = True
    critical_min_tool_calls: int = 3


class PostProcessingConfig(BaseModel):
    """Post-processing rules applied after agent execution."""
    min_confidence: float = 0.6
    max_findings_per_agent: int = 5
    evidence_gate: Optional[EvidenceGateConfig] = None


# ---------------------------------------------------------------------------
# Top-level workflow config
# ---------------------------------------------------------------------------


class WorkflowConfig(BaseModel):
    """Complete workflow configuration, loaded from a YAML file.

    This is the top-level model that represents everything needed to
    execute a workflow: budget, classifier, routes, pipeline stages,
    and post-processing rules.
    """
    name: str
    description: str = ""
    prompt_template: Optional[str] = None     # path to shared prompt .md file
    route_mode: Literal["first_match", "parallel_all_matching"]

    budget: BudgetDefaults = Field(default_factory=BudgetDefaults)
    core_tools: List[str] = Field(default_factory=list)

    dispatch: DispatchConfig
    routes: Dict[str, RouteConfig]

    # Shared stages after all parallel routes (parallel_all_matching only)
    post_pipeline: List[StageConfig] = Field(default_factory=list)

    post_processing: Optional[PostProcessingConfig] = None

    # Resolved data (populated by loader, not from YAML)
    prompt_template_content: Optional[str] = None
    resolved_agents: Dict[str, AgentConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_post_pipeline(self) -> "WorkflowConfig":
        if self.post_pipeline and self.route_mode != "parallel_all_matching":
            raise ValueError(
                "post_pipeline is only valid with route_mode='parallel_all_matching'"
            )
        return self


# ---------------------------------------------------------------------------
# Classifier result
# ---------------------------------------------------------------------------


class ClassifierResult(BaseModel):
    """Output from ClassifierEngine.classify()."""
    # For first_match: the best matching route name
    best_route: Optional[str] = None

    # For parallel_all_matching: all routes with their match levels
    matched_routes: Dict[str, str] = Field(default_factory=dict)  # route_name → level

    # Raw dimension scores (for debugging / logging)
    raw_scores: Dict[str, Any] = Field(default_factory=dict)
