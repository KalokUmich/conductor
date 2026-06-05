"""Pydantic models for the Brain orchestrator configs.

Validates:
  - Agent .md frontmatter (YAML between --- markers)
  - Brain configs (``brains/default.yaml``, ``brains/pr_review.yaml``)
  - Swarm preset configs (``swarms/*.yaml``)
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Agent config (parsed from .md frontmatter)
# ---------------------------------------------------------------------------


class ToolsConfig(BaseModel):
    """Tool configuration for an agent."""

    core: bool = True  # include workflow's core_tools
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

    evidence_check: bool = True  # run rule-based evidence check
    min_file_refs: int = 1  # minimum file:line references in answer
    min_tool_calls: int = 2  # minimum tool calls made
    need_brain_review: bool = False  # Brain evaluates findings before synthesis


class AgentConfig(BaseModel):
    """A single agent definition, parsed from a .md file with YAML frontmatter.

    Supports both legacy format (type/model_role/budget_weight/tools.core+extra)
    and new Brain format (model/limits/tools as flat list/description).
    """

    name: str
    type: Literal["explorer", "judge"] = "explorer"
    category: Optional[str] = None

    # --- New Brain format fields ---
    description: str = ""  # Brain reads this to match queries to agents
    model: Literal["explorer", "strong"] = "explorer"
    strategy: str = ""  # Layer 2 strategy key (e.g., "code_review")
    skill: str = ""  # Layer 3 investigation skill (e.g., "business_flow")
    focus: str = ""  # Focus directive prepended to query in swarm dispatch
    limits: AgentLimits = Field(default_factory=AgentLimits)
    quality: QualityConfig = Field(default_factory=QualityConfig)

    # --- Legacy format fields (kept for backward compat) ---
    model_role: Literal["explorer", "strong"] = "explorer"

    # Tools — new format: flat list; legacy: ToolsConfig with core+extra
    tools: Any = Field(default_factory=ToolsConfig)

    # Budget (legacy)
    budget_weight: float = 1.0
    max_tokens: Optional[int] = None  # judge agents only

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
    def _sync_model_fields(self) -> AgentConfig:
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
# Brain orchestrator config (loaded from brains/default.yaml)
# ---------------------------------------------------------------------------


class BrainLimits(BaseModel):
    """Resource limits for the Brain orchestrator."""

    max_iterations: int = 20
    budget_tokens: int = 100_000  # Brain's own budget
    total_session_tokens: int = 800_000  # total across Brain + sub-agents
    max_concurrent_agents: int = 3
    sub_agent_timeout: float = 300.0  # 5 minutes per sub-agent
    max_depth: int = 2  # Brain(0) → agent(1) → sub-agent(2)


class BrainConfig(BaseModel):
    """Brain orchestrator configuration, loaded from brains/default.yaml."""

    model: str = "strong"
    limits: BrainLimits = Field(default_factory=BrainLimits)
    core_tools: List[str] = Field(
        default_factory=lambda: [
            "grep",
            "read_file",
            "find_symbol",
            "file_outline",
            "compressed_view",
            "expand_symbol",
        ]
    )


# ---------------------------------------------------------------------------
# PR Brain config (loaded from brains/pr_review.yaml)
# ---------------------------------------------------------------------------


class PostProcessingConfig(BaseModel):
    """Post-processing settings for PR Brain."""

    min_confidence: float = 0.75
    max_findings: int = 10
    max_findings_per_agent: int = 3  # Per-agent cap before merge


class SynthesisConfig(BaseModel):
    """LLM synthesis call settings."""

    max_tokens: int = 4096  # Max output tokens for final review
    max_diff_chars: int = 30_000  # Total diff chars in synthesis prompt
    max_diff_snippet_chars: int = 4000  # Per-file diff snippet cap


class ArbitrationConfig(BaseModel):
    """Arbitration agent settings."""

    budget_tokens: int = 200_000  # Token budget for arbitrator agent
    max_tokens: int = 2048  # Max output tokens for lightweight arbitration


class PRBrainLimits(BrainLimits):
    """Extended limits for PR Brain (adds PR-specific fields to BrainLimits)."""

    llm_concurrency_limit: int = 2  # Max parallel LLM calls (Bedrock throttle guard)
    small_pr_threshold: int = 100  # PRs under this skip concurrency/reliability
    reject_above: int = 6000  # Max changed lines before rejecting PR


class PRBrainConfig(BaseModel):
    """PR Brain orchestrator configuration, loaded from brains/pr_review.yaml.

    All tunable parameters live here — code reads from config, not hardcoded
    constants. Edit ``config/brains/pr_review.yaml`` to tune without code changes.
    """

    name: str = "pr_review"
    description: str = ""
    model: str = "strong"
    limits: PRBrainLimits = Field(default_factory=PRBrainLimits)
    review_agents: List[str] = Field(
        default_factory=lambda: [
            "correctness",
            "concurrency",
            "security",
            "reliability",
            "test_coverage",
        ]
    )
    arbitrator: str = "pr_arbitrator"
    budget_weights: Dict[str, float] = Field(
        default_factory=lambda: {
            "correctness": 1.00,
            "concurrency": 0.85,
            "security": 0.75,
            "reliability": 0.70,
            "test_coverage": 0.55,
        }
    )
    post_processing: PostProcessingConfig = Field(default_factory=PostProcessingConfig)
    synthesis: SynthesisConfig = Field(default_factory=SynthesisConfig)
    arbitration: ArbitrationConfig = Field(default_factory=ArbitrationConfig)


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
