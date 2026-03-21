"""Workflow management API endpoints.

Endpoints:
  GET  /api/workflows                  — list all available workflows
  GET  /api/workflows/{name}           — get full workflow config
  GET  /api/workflows/{name}/mermaid   — get Mermaid diagram
  GET  /api/workflows/{name}/graph     — get React Flow-compatible graph JSON
  PUT  /api/workflows/{name}/models    — update model assignments
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .loader import load_all_workflows
from .mermaid import generate_mermaid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

# Cache loaded workflows (refreshed on server restart)
_workflows_cache: Optional[Dict[str, Any]] = None


def _get_workflows():
    """Load and cache all workflows."""
    global _workflows_cache
    if _workflows_cache is None:
        _workflows_cache = load_all_workflows()
    return _workflows_cache


def _get_workflow(name: str):
    """Get a single workflow by name."""
    workflows = _get_workflows()
    if name not in workflows:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    return workflows[name]


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class WorkflowSummary(BaseModel):
    name: str
    description: str
    route_mode: str
    route_count: int
    agent_count: int
    model_assignments: Dict[str, str] = {}


class AgentSummary(BaseModel):
    name: str
    type: str
    model_role: str
    category: Optional[str] = None
    tool_count: int = 0
    budget_weight: float = 1.0


class RouteSummary(BaseModel):
    name: str
    pattern_count: int = 0
    pipeline_stages: int = 0
    agents: List[str] = []
    is_delegate: bool = False
    delegate_to: Optional[str] = None


class WorkflowDetail(BaseModel):
    name: str
    description: str
    route_mode: str
    routes: List[RouteSummary]
    agents: List[AgentSummary]
    post_pipeline_agents: List[AgentSummary] = []
    mermaid: str = ""
    model_assignments: Dict[str, str] = {}


class ModelAssignment(BaseModel):
    explorer: Optional[str] = None
    judge: Optional[str] = None


class GraphNode(BaseModel):
    id: str
    type: str   # classifier, explorer, judge, group, start, end
    data: Dict[str, Any] = {}
    position: Optional[Dict[str, float]] = None


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str = ""


class WorkflowGraph(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=List[WorkflowSummary])
async def list_workflows():
    """List all available workflows."""
    workflows = _get_workflows()
    result = []
    for name, wf in workflows.items():
        result.append(WorkflowSummary(
            name=wf.name,
            description=wf.description,
            route_mode=wf.route_mode,
            route_count=len(wf.routes),
            agent_count=len(wf.resolved_agents),
        ))
    return result


@router.get("/{name}", response_model=WorkflowDetail)
async def get_workflow(name: str):
    """Get full workflow details."""
    wf = _get_workflow(name)

    routes = []
    for rname, route in wf.routes.items():
        agent_names = []
        for stage in route.pipeline:
            for agent_path in stage.agents:
                agent = wf.resolved_agents.get(agent_path)
                if agent:
                    agent_names.append(agent.name)
        routes.append(RouteSummary(
            name=rname,
            pattern_count=len(route.file_patterns or route.text_patterns),
            pipeline_stages=len(route.pipeline),
            agents=agent_names,
            is_delegate=route.delegate is not None,
            delegate_to=route.delegate,
        ))

    agents = []
    for path, agent in wf.resolved_agents.items():
        agents.append(AgentSummary(
            name=agent.name,
            type=agent.type,
            model_role=agent.model_role,
            category=agent.category,
            tool_count=len(agent.tools.extra),
            budget_weight=agent.budget_weight,
        ))

    post_agents = []
    for stage in wf.post_pipeline:
        for agent_path in stage.agents:
            agent = wf.resolved_agents.get(agent_path)
            if agent:
                post_agents.append(AgentSummary(
                    name=agent.name,
                    type=agent.type,
                    model_role=agent.model_role,
                    tool_count=len(agent.tools.extra),
                    budget_weight=agent.budget_weight,
                ))

    mermaid = generate_mermaid(wf)

    return WorkflowDetail(
        name=wf.name,
        description=wf.description,
        route_mode=wf.route_mode,
        routes=routes,
        agents=agents,
        post_pipeline_agents=post_agents,
        mermaid=mermaid,
    )


@router.get("/{name}/mermaid")
async def get_workflow_mermaid(name: str):
    """Get Mermaid diagram for a workflow."""
    wf = _get_workflow(name)
    return {"mermaid": generate_mermaid(wf)}


@router.get("/{name}/graph", response_model=WorkflowGraph)
async def get_workflow_graph(name: str):
    """Get React Flow-compatible graph JSON for a workflow."""
    wf = _get_workflow(name)
    return _build_graph(wf)


@router.put("/{name}/models")
async def update_workflow_models(name: str, assignment: ModelAssignment):
    """Update model assignments for a workflow.

    Stores the assignment in memory (will be persisted to settings YAML
    when full config persistence is implemented).
    """
    wf = _get_workflow(name)
    # Store in-memory for now
    # TODO: persist to conductor.settings.yaml workflow_models section
    result = {}
    if assignment.explorer:
        result["explorer"] = assignment.explorer
    if assignment.judge:
        result["judge"] = assignment.judge
    logger.info("Updated model assignments for '%s': %s", name, result)
    return {"status": "ok", "workflow": name, "models": result}


# ---------------------------------------------------------------------------
# Graph builder — positions computed by dagre on the frontend
# ---------------------------------------------------------------------------


def _build_graph(wf) -> WorkflowGraph:
    """Build a node/edge graph from a workflow.

    Positions are placeholders — the frontend uses dagre.js for layout.
    """
    nodes: List[GraphNode] = []
    edges: List[GraphEdge] = []
    _pos = {"x": 0, "y": 0}

    # Start node
    nodes.append(GraphNode(
        id="start",
        type="start",
        data={"label": wf.description or wf.name},
        position=_pos,
    ))

    # Classifier node
    classifier_type = wf.dispatch.classifier.type.replace("_", " ").title()
    nodes.append(GraphNode(
        id="classifier",
        type="classifier",
        data={"label": classifier_type, "route_count": len(wf.routes)},
        position=_pos,
    ))
    edges.append(GraphEdge(id="e-start-classify", source="start", target="classifier"))

    # Route nodes — multi-stage pipelines emit inter-stage edges
    route_ids: List[str] = []
    node_ids_seen: set = set()
    for rname, route in wf.routes.items():
        if route.delegate:
            node_id = f"route:{rname}"
            nodes.append(GraphNode(
                id=node_id, type="delegate",
                data={"label": rname, "delegate_to": route.delegate},
                position=_pos,
            ))
            patterns = route.text_patterns or route.file_patterns
            edges.append(GraphEdge(
                id=f"e-classify-{rname}", source="classifier", target=node_id,
                label=_short_pattern_label(patterns),
            ))
            route_ids.append(node_id)
            continue

        # Collect agents per stage for inter-stage edges
        prev_stage_ids: List[str] = []
        last_stage_ids: List[str] = []
        for stage_idx, stage in enumerate(route.pipeline):
            cur_stage_ids: List[str] = []
            for agent_path in stage.agents:
                agent = wf.resolved_agents.get(agent_path)
                if not agent:
                    continue
                node_id = f"agent:{agent.name}"
                if node_id not in node_ids_seen:
                    node_ids_seen.add(node_id)
                    instructions = ""
                    if hasattr(agent, "instructions") and agent.instructions:
                        instructions = agent.instructions[:300]
                    nodes.append(GraphNode(
                        id=node_id, type=agent.type,
                        data={
                            "label": agent.name,
                            "model_role": agent.model_role,
                            "tool_count": len(agent.tools.extra),
                            "tools": list(agent.tools.extra),
                            "budget_weight": agent.budget_weight,
                            "instructions": instructions,
                        },
                        position=_pos,
                    ))
                cur_stage_ids.append(node_id)

                # First stage: edge from classifier
                if stage_idx == 0:
                    patterns = route.text_patterns or route.file_patterns
                    label = _short_pattern_label(patterns)
                    if agent.trigger.always:
                        label = "always"
                    edges.append(GraphEdge(
                        id=f"e-classify-{rname}-{agent.name}",
                        source="classifier", target=node_id, label=label,
                    ))

            # Inter-stage edges: each agent in prev stage → each agent in this stage
            if stage_idx > 0 and prev_stage_ids:
                for src_id in prev_stage_ids:
                    for tgt_id in cur_stage_ids:
                        edges.append(GraphEdge(
                            id=f"e-{src_id}-{tgt_id}",
                            source=src_id, target=tgt_id,
                        ))

            prev_stage_ids = cur_stage_ids
            last_stage_ids = cur_stage_ids

        # Terminal nodes of this route (last stage agents)
        route_ids.extend(last_stage_ids)

    # Post-pipeline nodes
    if wf.post_pipeline:
        merge_id = "merge"
        nodes.append(GraphNode(
            id=merge_id, type="merge",
            data={"label": "Merge + Filter"}, position=_pos,
        ))
        for rid in route_ids:
            edges.append(GraphEdge(
                id=f"e-{rid}-merge", source=rid, target=merge_id,
            ))

        prev_id = merge_id
        for stage in wf.post_pipeline:
            for agent_path in stage.agents:
                agent = wf.resolved_agents.get(agent_path)
                if not agent:
                    continue
                node_id = f"agent:{agent.name}"
                instructions = ""
                if hasattr(agent, "instructions") and agent.instructions:
                    instructions = agent.instructions[:300]
                nodes.append(GraphNode(
                    id=node_id, type=agent.type,
                    data={
                        "label": agent.name,
                        "model_role": agent.model_role,
                        "tools": list(agent.tools.extra) if agent.tools else [],
                        "instructions": instructions,
                    },
                    position=_pos,
                ))
                edges.append(GraphEdge(
                    id=f"e-{prev_id}-{agent.name}", source=prev_id, target=node_id,
                ))
                prev_id = node_id

        nodes.append(GraphNode(
            id="end", type="end",
            data={"label": "Result"}, position=_pos,
        ))
        edges.append(GraphEdge(
            id=f"e-{prev_id}-end", source=prev_id, target="end",
        ))
    else:
        # first_match: all routes lead to answer
        nodes.append(GraphNode(
            id="end", type="end",
            data={"label": "Answer"}, position=_pos,
        ))
        for rid in route_ids:
            edges.append(GraphEdge(
                id=f"e-{rid}-end", source=rid, target="end",
            ))

    return WorkflowGraph(nodes=nodes, edges=edges)


def _short_pattern_label(patterns: List[str], max_items: int = 3) -> str:
    """Extract short keywords from patterns for edge labels."""
    if not patterns:
        return ""
    # Take first pattern, split on |, take first few keywords
    first = patterns[0]
    keywords = [k.strip() for k in first.split("|")][:max_items]
    label = "/".join(keywords)
    if len(patterns) > 1 or len(first.split("|")) > max_items:
        label += "/..."
    return label
