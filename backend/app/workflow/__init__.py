"""Brain-mode orchestration support.

Modules:
  models.py            — Pydantic schemas for agent .md frontmatter + brain configs
  loader.py            — Load agent / brain / swarm configs from disk
  engine.py            — WorkflowEngine: hosts the Brain orchestrator entry point
  router.py            — FastAPI endpoints for the Agent Swarm UI tab (/api/brain/*)
"""
