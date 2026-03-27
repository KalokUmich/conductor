"""Workflow and agent config loader.

Loads workflow YAML files and agent .md files (YAML frontmatter + Markdown body)
from the config directory. Resolves agent references, delegate workflows,
core tool expansion, and validates input/output declarations.

Config file search order:
  1. ./config/{path}
  2. ../config/{path}
  3. ~/.conductor/{path}
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

from .models import (
    AgentConfig,
    AgentLimits,
    BrainConfig,
    DispatchConfig,
    QualityConfig,
    RouteConfig,
    StageConfig,
    SwarmConfig,
    ToolsConfig,
    TriggerConfig,
    WorkflowConfig,
)

logger = logging.getLogger(__name__)

# Regex to split YAML frontmatter from Markdown body
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n(.*)",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Config file discovery
# ---------------------------------------------------------------------------

_CONFIG_SEARCH_DIRS: List[str] = []


def _find_config_dir() -> Path:
    """Find the config directory. Caches the result."""
    if _CONFIG_SEARCH_DIRS:
        return Path(_CONFIG_SEARCH_DIRS[0])

    candidates = [
        Path.cwd() / "config",
        Path.cwd().parent / "config",
        Path.home() / ".conductor",
    ]
    for p in candidates:
        if p.is_dir():
            _CONFIG_SEARCH_DIRS.append(str(p))
            return p

    # Fallback: use ./config even if it doesn't exist yet
    fallback = Path.cwd() / "config"
    _CONFIG_SEARCH_DIRS.append(str(fallback))
    return fallback


def _resolve_path(relative_path: str) -> Path:
    """Resolve a config-relative path to an absolute path."""
    config_dir = _find_config_dir()
    resolved = config_dir / relative_path
    if not resolved.exists():
        raise FileNotFoundError(
            f"Config file not found: {relative_path}\n"
            f"Searched in: {config_dir}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Agent loader
# ---------------------------------------------------------------------------


def load_agent(path: str) -> AgentConfig:
    """Load an agent definition from a .md file with YAML frontmatter.

    Args:
        path: Config-relative path (e.g. "agents/security.md").

    Returns:
        Populated AgentConfig with instructions from the Markdown body.

    Raises:
        FileNotFoundError: If the agent file doesn't exist.
        ValueError: If frontmatter is missing or invalid.
    """
    resolved = _resolve_path(path)
    content = resolved.read_text(encoding="utf-8")

    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise ValueError(
            f"Agent file missing YAML frontmatter (--- markers): {path}"
        )

    frontmatter_text = match.group(1)
    body = match.group(2).strip()

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML frontmatter in {path}: {exc}") from exc

    if not isinstance(frontmatter, dict):
        raise ValueError(f"Frontmatter must be a YAML mapping in {path}")

    # Normalize tools config — supports both formats:
    #   New (flat list): tools: [grep, read_file, ...]
    #   Legacy (dict):   tools: {core: true, extra: [...]}
    tools_raw = frontmatter.pop("tools", {})
    if isinstance(tools_raw, list):
        tools = tools_raw  # new Brain format: flat list
    elif isinstance(tools_raw, dict):
        tools = ToolsConfig(**tools_raw)  # legacy format
    else:
        tools = ToolsConfig()

    # Normalize trigger config
    trigger_raw = frontmatter.pop("trigger", {})
    if isinstance(trigger_raw, dict):
        trigger = TriggerConfig(**trigger_raw)
    else:
        trigger = TriggerConfig()

    # Normalize limits config (new Brain format)
    limits_raw = frontmatter.pop("limits", {})
    if isinstance(limits_raw, dict):
        limits = AgentLimits(**limits_raw)
    else:
        limits = AgentLimits()

    # Normalize quality config
    quality_raw = frontmatter.pop("quality", {})
    if isinstance(quality_raw, dict):
        quality = QualityConfig(**quality_raw)
    else:
        quality = QualityConfig()

    return AgentConfig(
        tools=tools,
        trigger=trigger,
        limits=limits,
        quality=quality,
        instructions=body,
        source_path=str(resolved),
        **frontmatter,
    )


# ---------------------------------------------------------------------------
# Workflow loader
# ---------------------------------------------------------------------------


def load_workflow(
    path: str,
    *,
    _loaded: Optional[Set[str]] = None,
) -> WorkflowConfig:
    """Load a workflow definition from a YAML file.

    Resolves all agent file references and delegate workflows.
    Detects circular delegate references.

    Args:
        path: Config-relative path (e.g. "workflows/pr_review.yaml").
        _loaded: Internal set for circular reference detection.

    Returns:
        Fully populated WorkflowConfig.

    Raises:
        FileNotFoundError: If workflow or agent files don't exist.
        ValueError: If validation fails (circular refs, bad input/output, etc.).
    """
    if _loaded is None:
        _loaded = set()

    # Circular reference detection
    if path in _loaded:
        raise ValueError(f"Circular workflow delegate detected: {path}")
    _loaded.add(path)

    resolved = _resolve_path(path)
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Workflow YAML must be a mapping: {path}")

    # Load shared prompt template
    prompt_content = None
    prompt_path = raw.get("prompt_template")
    if prompt_path:
        try:
            prompt_file = _resolve_path(prompt_path)
            prompt_content = prompt_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("Prompt template not found: %s", prompt_path)

    # Parse the workflow config
    workflow = WorkflowConfig(**raw)
    workflow.prompt_template_content = prompt_content

    # Resolve all agent references
    core_tools = workflow.core_tools
    all_agents: Dict[str, AgentConfig] = {}

    def _resolve_agents_in_stages(stages: List[StageConfig]) -> None:
        for stage in stages:
            resolved_paths = []
            for agent_path in stage.agents:
                if agent_path not in all_agents:
                    agent = load_agent(agent_path)
                    # Expand core tools
                    if agent.tools.core:
                        full_tools = list(core_tools) + [
                            t for t in agent.tools.extra if t not in core_tools
                        ]
                        agent.tools.extra = full_tools
                        agent.tools.core = False  # mark as resolved
                    all_agents[agent_path] = agent
                resolved_paths.append(agent_path)
            stage.agents = resolved_paths

    # Resolve agents in route pipelines
    for route_name, route in workflow.routes.items():
        if route.delegate:
            # Load delegate workflow (recursively, with cycle detection)
            try:
                delegate_wf = load_workflow(route.delegate, _loaded=_loaded)
                # Store the delegate workflow's resolved agents too
                all_agents.update(delegate_wf.resolved_agents)
            except (FileNotFoundError, ValueError) as exc:
                logger.warning(
                    "Failed to load delegate workflow %s for route %s: %s",
                    route.delegate, route_name, exc,
                )
        else:
            _resolve_agents_in_stages(route.pipeline)

    # Resolve agents in post_pipeline
    _resolve_agents_in_stages(workflow.post_pipeline)

    workflow.resolved_agents = all_agents

    # Validate input/output declarations
    _validate_io(workflow)

    logger.info(
        "Loaded workflow '%s': %d routes, %d agents, route_mode=%s",
        workflow.name, len(workflow.routes), len(all_agents), workflow.route_mode,
    )
    return workflow


# ---------------------------------------------------------------------------
# Input/output validation
# ---------------------------------------------------------------------------

# Outputs that are always available (produced by code, not agents)
_IMPLICIT_OUTPUTS = {
    "query", "diffs", "risk_profile", "file_list", "impact_context",
    "workspace_layout", "pr_context", "diff_snippets",
}


def _validate_io(workflow: WorkflowConfig) -> None:
    """Validate that each agent's declared inputs are available at its stage.

    An input is available if:
      - It's in _IMPLICIT_OUTPUTS (runtime-provided)
      - It was declared as output by an agent in an earlier stage
    """
    for route_name, route in workflow.routes.items():
        if route.delegate:
            continue
        _validate_pipeline_io(route.pipeline, route_name, workflow)

    if workflow.post_pipeline:
        # post_pipeline runs after all routes, so all route outputs are available
        _validate_pipeline_io(
            workflow.post_pipeline, "post_pipeline", workflow,
            extra_available={"findings", "perspective_answers", "raw_evidence",
                             "perspective_answer", "answer"},
        )


def _validate_pipeline_io(
    stages: List[StageConfig],
    context_name: str,
    workflow: WorkflowConfig,
    extra_available: Optional[Set[str]] = None,
) -> None:
    """Validate input/output flow through a pipeline's stages."""
    available = set(_IMPLICIT_OUTPUTS)
    if extra_available:
        available |= extra_available

    for stage in stages:
        for agent_path in stage.agents:
            agent = workflow.resolved_agents.get(agent_path)
            if not agent:
                continue

            missing = set(agent.input) - available
            if missing:
                logger.warning(
                    "Agent '%s' in %s.%s declares inputs %s not available "
                    "from previous stages. Available: %s",
                    agent.name, context_name, stage.stage,
                    missing, available,
                )

        # After this stage, add all agent outputs to available set
        for agent_path in stage.agents:
            agent = workflow.resolved_agents.get(agent_path)
            if agent and agent.output:
                available.add(agent.output)


# ---------------------------------------------------------------------------
# Convenience: load all workflows from config/workflows/
# ---------------------------------------------------------------------------


def load_all_workflows() -> Dict[str, WorkflowConfig]:
    """Load all workflow YAML files from the workflows/ directory.

    Returns:
        Dict mapping workflow name to WorkflowConfig.
    """
    config_dir = _find_config_dir()
    workflows_dir = config_dir / "workflows"
    if not workflows_dir.is_dir():
        logger.warning("No workflows directory found at %s", workflows_dir)
        return {}

    result = {}
    for yaml_file in sorted(workflows_dir.glob("*.yaml")):
        rel_path = f"workflows/{yaml_file.name}"
        try:
            wf = load_workflow(rel_path)
            result[wf.name] = wf
        except Exception as exc:
            logger.error("Failed to load workflow %s: %s", rel_path, exc)

    return result


# ---------------------------------------------------------------------------
# Brain config loader
# ---------------------------------------------------------------------------


def load_brain_config() -> BrainConfig:
    """Load the Brain orchestrator configuration from brain.yaml.

    Returns:
        BrainConfig with limits, core_tools, and model settings.
        Falls back to defaults if brain.yaml doesn't exist.
    """
    try:
        resolved = _resolve_path("brain.yaml")
        data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("brain.yaml is not a mapping, using defaults")
            return BrainConfig()
        return BrainConfig(**data)
    except FileNotFoundError:
        logger.info("brain.yaml not found, using default Brain config")
        return BrainConfig()
    except Exception as exc:
        logger.warning("Failed to load brain.yaml: %s — using defaults", exc)
        return BrainConfig()


# ---------------------------------------------------------------------------
# Swarm preset loader
# ---------------------------------------------------------------------------


def load_swarm(path: str) -> SwarmConfig:
    """Load a swarm preset from a YAML file.

    Args:
        path: Config-relative path (e.g. "swarms/pr_review.yaml").

    Returns:
        SwarmConfig with agent list and mode.
    """
    resolved = _resolve_path(path)
    data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Swarm file must be a YAML mapping: {path}")
    return SwarmConfig(**data)


def load_swarm_registry() -> Dict[str, SwarmConfig]:
    """Load all swarm presets from config/swarms/*.yaml.

    Returns:
        Dict mapping swarm name to SwarmConfig.
    """
    config_dir = _find_config_dir()
    swarms_dir = config_dir / "swarms"
    if not swarms_dir.is_dir():
        logger.info("No swarms directory found at %s", swarms_dir)
        return {}

    result: Dict[str, SwarmConfig] = {}
    for yaml_file in sorted(swarms_dir.glob("*.yaml")):
        rel_path = f"swarms/{yaml_file.name}"
        try:
            swarm = load_swarm(rel_path)
            result[swarm.name] = swarm
            logger.info("Loaded swarm '%s': %d agents, mode=%s",
                        swarm.name, len(swarm.agents), swarm.mode)
        except Exception as exc:
            logger.error("Failed to load swarm %s: %s", rel_path, exc)

    return result


# ---------------------------------------------------------------------------
# Agent registry loader (for Brain mode)
# ---------------------------------------------------------------------------


def load_agent_registry() -> Dict[str, AgentConfig]:
    """Load all agent definitions from config/agents/*.md.

    Returns:
        Dict mapping agent name to AgentConfig.
    """
    config_dir = _find_config_dir()
    agents_dir = config_dir / "agents"
    if not agents_dir.is_dir():
        logger.warning("No agents directory found at %s", agents_dir)
        return {}

    result: Dict[str, AgentConfig] = {}
    for md_file in sorted(agents_dir.glob("*.md")):
        rel_path = f"agents/{md_file.name}"
        try:
            agent = load_agent(rel_path)
            result[agent.name] = agent
        except Exception as exc:
            logger.error("Failed to load agent %s: %s", rel_path, exc)

    logger.info("Loaded agent registry: %d agents", len(result))
    return result
