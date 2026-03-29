"""Generate Mermaid flowchart diagrams from WorkflowConfig objects.

Supports both route modes:
  - parallel_all_matching: all matching routes run in parallel, then post_pipeline
  - first_match: first matching route wins, single pipeline

Usage:
    from app.workflow.mermaid import generate_mermaid
    from app.workflow.loader import load_workflow

    wf = load_workflow("workflows/pr_review.yaml")
    print(generate_mermaid(wf))
"""
from __future__ import annotations

from typing import List, Optional, Set

from .models import AgentConfig, RouteConfig, WorkflowConfig


# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------

_STYLE_EXPLORER = "fill:#1e1b4b,stroke:#8b5cf6"
_STYLE_JUDGE = "fill:#312e81,stroke:#c4b5fd"

# Max routes to show before collapsing into "...other routes"
_MAX_ROUTES = 6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_id(name: str) -> str:
    """Turn an agent/route name into a valid Mermaid node ID."""
    return name.replace("-", "_").replace(".", "_").replace("/", "_")


def _agent_label(agent: AgentConfig) -> str:
    """Build a human-readable node label for an agent.

    Shows name, type, and either tool count (explorer) or model_role (judge).
    """
    parts = [agent.name]
    parts.append(agent.type)
    if agent.type == "explorer":
        tool_count = len(agent.tools.extra)
        if agent.budget_weight != 1.0:
            parts.append(f"weight: {agent.budget_weight}")
        parts.append(f"{tool_count} tools")
    else:
        # Judge: show model_role
        parts.append(agent.model_role)
    return "\\n".join(parts)


def _edge_label_from_patterns(route_name: str, route: RouteConfig) -> str:
    """Extract a short edge label from text_patterns or file_patterns."""
    if route.text_patterns:
        # Take first pattern, split on |, pick first 2-3 keywords
        first = route.text_patterns[0]
        keywords = [k.strip() for k in first.split("|")][:3]
        return "/".join(keywords)
    # For risk_pattern classifier, use the route name as-is
    return route_name


def _trigger_label(agent: AgentConfig, route_name: str) -> str:
    """Build the edge label for a parallel_all_matching route."""
    if agent.trigger.always:
        return "always"
    dims = agent.trigger.risk_dimensions
    if dims:
        dim_str = ", ".join(dims)
        return f'"{dim_str} >= medium"'
    return f'"{route_name}"'


def _resolve_agent(
    wf: WorkflowConfig, agent_path: str
) -> Optional[AgentConfig]:
    """Look up an agent from the workflow's resolved_agents dict."""
    return wf.resolved_agents.get(agent_path)


# ---------------------------------------------------------------------------
# parallel_all_matching mode (e.g. PR Review)
# ---------------------------------------------------------------------------


def _generate_parallel_all_matching(wf: WorkflowConfig) -> str:
    """Generate Mermaid for parallel_all_matching workflows (PR Review)."""
    lines: List[str] = ["graph TD"]

    # Classifier type label
    clf_type = wf.dispatch.classifier.type.replace("_", " ").title()
    start_label = wf.description or wf.name
    lines.append(f'    START(["{start_label}"]) --> CLASSIFY{{"{clf_type}"}}')

    # Collect all route agent node IDs for the merge point
    route_node_ids: List[str] = []
    style_lines: List[str] = []

    route_items = list(wf.routes.items())
    shown_routes = route_items[:_MAX_ROUTES]
    has_overflow = len(route_items) > _MAX_ROUTES

    seen_node_ids: Set[str] = set()

    for route_name, route in shown_routes:
        if route.delegate:
            node_id = _sanitize_id(route_name)
            lines.append(
                f'    CLASSIFY -->|"{route_name}"| {node_id}["{route_name}\\ndelegate"]'
            )
            route_node_ids.append(node_id)
            continue

        # Each route has a pipeline; for PR review, typically one explore stage
        # with one agent. Gather the first agent for label/trigger info.
        first_agent: Optional[AgentConfig] = None
        stage_node_ids: List[str] = []

        for stage in route.pipeline:
            for agent_path in stage.agents:
                agent = _resolve_agent(wf, agent_path)
                if not agent:
                    continue
                if first_agent is None:
                    first_agent = agent

                # Use route_name prefix to avoid duplicate IDs when the
                # same agent serves multiple routes (e.g. reliability
                # used for both reliability and operational routes).
                base_id = _sanitize_id(agent.name)
                node_id = base_id if base_id not in seen_node_ids else f"{_sanitize_id(route_name)}_{base_id}"
                seen_node_ids.add(node_id)

                tool_count = len(agent.tools.extra)
                weight_str = f"weight: {agent.budget_weight}" if agent.budget_weight != 1.0 else ""
                label_parts = [agent.name]
                if weight_str:
                    label_parts.append(weight_str)
                label_parts.append(f"{tool_count} tools")
                label = "\\n".join(label_parts)

                edge_label = _trigger_label(agent, route_name)
                lines.append(
                    f'    CLASSIFY -->|{edge_label}| {node_id}["{label}"]'
                )
                stage_node_ids.append(node_id)
                style_lines.append(
                    f"    style {node_id} {_STYLE_EXPLORER}"
                )

        route_node_ids.extend(stage_node_ids)

    if has_overflow:
        lines.append(
            '    CLASSIFY -->|"..."| OTHER["...other routes"]'
        )
        route_node_ids.append("OTHER")

    # Merge point before post_pipeline
    if route_node_ids and wf.post_pipeline:
        merge_ids = " & ".join(route_node_ids)
        lines.append(f"    {merge_ids} --> POST[Dedup + Filter]")

    # Post-pipeline stages (sequential)
    prev_id = "POST"
    for stage in wf.post_pipeline:
        for agent_path in stage.agents:
            agent = _resolve_agent(wf, agent_path)
            if not agent:
                continue
            node_id = _sanitize_id(agent.name)
            label_parts = [agent.name]
            label_parts.append(f"{agent.type} · {agent.model_role}")
            label = "\\n".join(label_parts)
            lines.append(f'    {prev_id} --> {node_id}["{label}"]')
            style_lines.append(f"    style {node_id} {_STYLE_JUDGE}")
            prev_id = node_id

    # Terminal node
    lines.append(f'    {prev_id} --> RESULT(["Review Result"])')

    # Append style lines
    lines.append("")
    lines.extend(style_lines)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# first_match mode (e.g. Code Explorer)
# ---------------------------------------------------------------------------


def _has_multi_stage_pipeline(route: RouteConfig) -> bool:
    """Check if a route has a multi-stage pipeline (needs a subgraph)."""
    if route.delegate:
        return False
    return len(route.pipeline) > 1 or any(
        len(s.agents) > 1 for s in route.pipeline
    )


def _generate_first_match(wf: WorkflowConfig) -> str:
    """Generate Mermaid for first_match workflows (Code Explorer)."""
    lines: List[str] = ["graph TD"]
    style_lines: List[str] = []

    # Classifier type label
    clf_type = wf.dispatch.classifier.type.replace("_", " ").title()
    start_label = wf.description or wf.name
    lines.append(f'    START(["{start_label}"]) --> CLASSIFY{{"{clf_type}"}}')

    # Collect all terminal node IDs to merge at the end
    terminal_ids: List[str] = []
    seen_node_ids: Set[str] = set()

    route_items = list(wf.routes.items())
    shown_routes = route_items[:_MAX_ROUTES]
    has_overflow = len(route_items) > _MAX_ROUTES

    for route_name, route in shown_routes:
        edge_label = _edge_label_from_patterns(route_name, route)
        route_id = _sanitize_id(route_name)

        if route.delegate:
            node_id = f"delegate_{route_id}"
            lines.append(
                f'    CLASSIFY -->|"{edge_label}"| {node_id}["{route_name}\\ndelegate"]'
            )
            terminal_ids.append(node_id)
            continue

        if _has_multi_stage_pipeline(route):
            # Multi-stage pipeline: use a subgraph
            group_id = f"{route_id}_GROUP"
            group_label = route_name
            lines.append(
                f'    CLASSIFY -->|"{edge_label}"| {group_id}'
            )
            lines.append(f'    subgraph {group_id}["{group_label}"]')

            # Render stages within the subgraph
            prev_stage_ids: List[str] = []

            for stage in route.pipeline:
                current_stage_ids: List[str] = []
                for agent_path in stage.agents:
                    agent = _resolve_agent(wf, agent_path)
                    if not agent:
                        continue
                    base_id = _sanitize_id(agent.name)
                    node_id = base_id if base_id not in seen_node_ids else f"{route_id}_{base_id}"
                    seen_node_ids.add(node_id)

                    label = _agent_label(agent)
                    lines.append(f'        {node_id}["{label}"]')
                    current_stage_ids.append(node_id)

                    if agent.type == "explorer":
                        style_lines.append(
                            f"    style {node_id} {_STYLE_EXPLORER}"
                        )
                    else:
                        style_lines.append(
                            f"    style {node_id} {_STYLE_JUDGE}"
                        )

                # Connect previous stage to current stage
                if prev_stage_ids:
                    for prev_id in prev_stage_ids:
                        for curr_id in current_stage_ids:
                            lines.append(
                                f"        {prev_id} --> {curr_id}"
                            )

                prev_stage_ids = current_stage_ids

            lines.append("    end")
            terminal_ids.append(group_id)

        else:
            # Single-stage, single-agent route: simple node
            stage = route.pipeline[0]
            if not stage.agents:
                continue
            agent_path = stage.agents[0]
            agent = _resolve_agent(wf, agent_path)
            if not agent:
                continue
            base_id = _sanitize_id(agent.name)
            node_id = base_id if base_id not in seen_node_ids else f"{route_id}_{base_id}"
            seen_node_ids.add(node_id)

            label = _agent_label(agent)
            lines.append(
                f'    CLASSIFY -->|"{edge_label}"| {node_id}["{label}"]'
            )
            terminal_ids.append(node_id)

            if agent.type == "explorer":
                style_lines.append(
                    f"    style {node_id} {_STYLE_EXPLORER}"
                )
            else:
                style_lines.append(
                    f"    style {node_id} {_STYLE_JUDGE}"
                )

    if has_overflow:
        lines.append(
            '    CLASSIFY -->|"..."| OTHER["...other routes"]'
        )
        terminal_ids.append("OTHER")

    # All routes converge to the answer
    if terminal_ids:
        merge_ids = " & ".join(terminal_ids)
        lines.append(f'    {merge_ids} --> ANSWER(["Answer"])')

    # Append style lines
    lines.append("")
    lines.extend(style_lines)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_mermaid(workflow: WorkflowConfig) -> str:
    """Generate a Mermaid flowchart from a workflow config.

    Args:
        workflow: A fully loaded WorkflowConfig (with resolved_agents populated).

    Returns:
        A string containing a valid Mermaid flowchart definition.
    """
    if workflow.route_mode == "parallel_all_matching":
        return _generate_parallel_all_matching(workflow)
    else:
        return _generate_first_match(workflow)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from .loader import load_workflow

    for name in ["workflows/pr_review.yaml", "workflows/code_explorer.yaml"]:
        wf = load_workflow(name)
        print(f"\n--- {wf.name} ---")  # CLI output: intentional print — dev tool for inspecting generated diagrams
        print(generate_mermaid(wf))
