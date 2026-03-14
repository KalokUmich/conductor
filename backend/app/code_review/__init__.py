"""AI Code Review module — multi-agent PR review system.

Orchestrates specialized review agents (correctness, concurrency, security,
reliability, test coverage) over a PR diff, then merges, deduplicates, and
ranks findings into a structured review.

Usage:
    from app.code_review.service import CodeReviewService

    service = CodeReviewService(
        provider=opus_provider,
        classifier_provider=haiku_provider,
    )
    result = await service.review(
        workspace_path="/path/to/ws",
        diff_spec="main...feature/branch",
    )
"""
