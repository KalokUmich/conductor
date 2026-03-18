"""Config-driven workflow engine for multi-agent orchestration.

Modules:
  models.py            — Pydantic schemas for workflow YAML + agent .md frontmatter
  loader.py            — Load and validate workflow configs from disk
  classifier_engine.py — Generic classifier (risk_pattern, keyword_pattern)
  engine.py            — WorkflowEngine: execute pipelines, dispatch agents
  mermaid.py           — Generate Mermaid diagrams from workflow configs
  router.py            — FastAPI endpoints for workflow management
"""
