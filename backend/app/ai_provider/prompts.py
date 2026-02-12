"""Prompt templates for AI providers.

This module contains shared prompt templates used by AI providers
for various tasks like summarization.
"""
from typing import Literal

# Discussion types for classification
DiscussionType = Literal[
    "api_design",
    "product_flow",
    "code_change",
    "architecture",
    "innovation",
    "debugging",
    "general"
]

# =============================================================================
# Stage 1: Classification Prompt
# =============================================================================

CLASSIFICATION_PROMPT = """You are an AI assistant that classifies software engineering discussions.

## Conversation
{conversation}

## Task
Analyze the conversation and classify it into ONE of the following categories:

1. **api_design** - Discussions about API endpoints, request/response formats, REST/GraphQL design, versioning
2. **product_flow** - Discussions about user flows, feature requirements, UX decisions, product behavior
3. **code_change** - Discussions about specific code modifications, refactoring, bug fixes, implementation details
4. **architecture** - Discussions about system design, component structure, scalability, infrastructure
5. **innovation** - Discussions about new ideas, experiments, proof of concepts, research
6. **debugging** - Discussions about investigating issues, error analysis, troubleshooting, root cause analysis
7. **general** - Discussions that don't fit clearly into the above categories

## Output Requirements
Your output must be ONLY valid JSON with no markdown formatting, no code blocks, and no additional explanation.

## Required JSON Schema
{{
  "discussion_type": "one of: api_design, product_flow, code_change, architecture, innovation, debugging, general",
  "confidence": 0.0 to 1.0 (how confident you are in this classification)
}}

Output only the JSON object:"""


# =============================================================================
# Stage 2: Specialized Summary Prompts by Discussion Type
# =============================================================================

# Base template structure for all specialized prompts
_SUMMARY_BASE = """You are an AI assistant for software engineering decision summarization.

## Conversation
{conversation}

## Discussion Type
This conversation has been classified as: **{discussion_type}**

{specialized_instructions}

## Output Requirements
Your output must be ONLY valid JSON with no markdown formatting, no code blocks, and no additional explanation.

## Required JSON Schema
{{
  "type": "decision_summary",
  "topic": "Brief topic of the discussion (1-2 sentences max)",
  "core_problem": "Clear description of the core problem or challenge",
  "proposed_solution": "The proposed solution, approach, or decision made",
  "requires_code_change": true or false,
  "impact_scope": "local" or "module" or "system" or "cross-system",
  "affected_components": ["list", "of", "affected", "components"],
  "risk_level": "low" or "medium" or "high",
  "next_steps": ["actionable", "items", "or", "follow-up", "tasks"]
}}

## Impact Scope Guidelines
- "local": Changes affect only a single function or class
- "module": Changes affect multiple files within one module/package
- "system": Changes affect multiple modules or the entire application
- "cross-system": Changes affect multiple systems or external integrations

## Risk Level Guidelines
- "low": Minor changes, well-understood scope, minimal dependencies
- "medium": Moderate changes, some complexity, affects multiple components
- "high": Major changes, high complexity, critical systems, or unclear scope

Output only the JSON object:"""

# Specialized instructions for each discussion type
SPECIALIZED_INSTRUCTIONS = {
    "api_design": """## API Design Focus
Pay special attention to:
1. Endpoint paths and HTTP methods discussed
2. Request/response payload structures
3. Authentication and authorization requirements
4. Versioning and backward compatibility concerns
5. Rate limiting or performance considerations

For requires_code_change: Set to TRUE if new endpoints need to be created or existing ones modified.
For impact_scope: Consider how many services/clients will be affected by API changes.""",

    "product_flow": """## Product Flow Focus
Pay special attention to:
1. User journey and interaction patterns
2. Feature requirements and acceptance criteria
3. Edge cases and error handling from user perspective
4. Integration with existing features
5. Data requirements and state management

For requires_code_change: Set to TRUE if UI/UX changes or new features are needed.
For impact_scope: Consider frontend, backend, and any data model changes.""",

    "code_change": """## Code Change Focus
Pay special attention to:
1. Specific files, functions, or classes mentioned
2. Implementation approach and patterns
3. Dependencies and imports affected
4. Test coverage requirements
5. Performance implications

For requires_code_change: Almost always TRUE for this discussion type.
For impact_scope: Assess based on the number of files and modules touched.""",

    "architecture": """## Architecture Focus
Pay special attention to:
1. System components and their interactions
2. Data flow and storage decisions
3. Scalability and performance considerations
4. Technology choices and trade-offs
5. Migration or transition plans

For requires_code_change: Set to TRUE if architectural changes require implementation.
For impact_scope: Usually "system" or "cross-system" for architecture discussions.""",

    "innovation": """## Innovation Focus
Pay special attention to:
1. Novel approaches or technologies being explored
2. Proof of concept requirements
3. Risks and unknowns
4. Success criteria and metrics
5. Resource and timeline estimates

For requires_code_change: Set to TRUE if prototyping or experimentation code is needed.
For impact_scope: Consider if this is isolated experimentation or broader integration.""",

    "debugging": """## Debugging Focus
Pay special attention to:
1. Error messages, stack traces, or symptoms described
2. Steps to reproduce the issue
3. Root cause analysis and findings
4. Proposed fix or workaround
5. Prevention measures for the future

For requires_code_change: Set to TRUE if a fix needs to be implemented.
For impact_scope: Assess based on whether the bug is localized or systemic.""",

    "general": """## General Discussion Focus
Extract the most relevant information:
1. Main topic and context
2. Key decisions or conclusions
3. Action items identified
4. Any technical implications
5. Follow-up requirements

For requires_code_change: Carefully assess if any code modifications were discussed.
For impact_scope: Use best judgment based on the discussion content.""",
}


def get_classification_prompt(messages: list) -> str:
    """Generate a classification prompt for the given messages.

    Args:
        messages: List of chat message objects.

    Returns:
        Complete classification prompt string ready for AI model input.
    """
    conversation = format_conversation(messages)
    return CLASSIFICATION_PROMPT.format(conversation=conversation)


def get_targeted_summary_prompt(messages: list, discussion_type: DiscussionType) -> str:
    """Generate a targeted summary prompt based on discussion type.

    Args:
        messages: List of chat message objects.
        discussion_type: The classified discussion type.

    Returns:
        Complete summary prompt string with specialized instructions.
    """
    conversation = format_conversation(messages)
    specialized_instructions = SPECIALIZED_INSTRUCTIONS.get(
        discussion_type,
        SPECIALIZED_INSTRUCTIONS["general"]
    )
    return _SUMMARY_BASE.format(
        conversation=conversation,
        discussion_type=discussion_type,
        specialized_instructions=specialized_instructions
    )


# =============================================================================
# Legacy: Original Structured Summary Prompt (kept for backward compatibility)
# =============================================================================

# Prompt template for structured decision summarization
STRUCTURED_SUMMARY_PROMPT = """You are an AI assistant for software engineering decision summarization.

Your task is to analyze the following conversation between a host and an engineer, then extract key information about problems discussed, decisions made, and whether code changes are required.

## Conversation
{conversation}

## Instructions
1. Identify the main topic or subject being discussed
2. Extract the core problem or challenge being addressed
3. Summarize the proposed solution or approach (if any)
4. Determine if code changes are required based on the discussion
5. List any components, files, or systems that would be affected
6. Assess the risk level based on scope and complexity
7. Extract clear action items or next steps

## Output Requirements
Your output must be ONLY valid JSON with no markdown formatting, no code blocks, and no additional explanation.

If the conversation lacks sufficient information for a field:
- Use an empty string "" for text fields where no information is available
- Use false for requires_code_change if unclear
- Use an empty array [] for lists with no items
- Use "low" for risk_level if assessment is not possible
- For topic, provide "No clear topic identified" if truly unclear

## Required JSON Schema
{{
  "type": "decision_summary",
  "topic": "Brief topic of the discussion (1-2 sentences max)",
  "problem_statement": "Clear description of the problem or challenge being discussed",
  "proposed_solution": "The proposed solution, approach, or decision made",
  "requires_code_change": true or false,
  "affected_components": ["list", "of", "affected", "components", "files", "or", "systems"],
  "risk_level": "low" or "medium" or "high",
  "next_steps": ["actionable", "items", "or", "follow-up", "tasks"]
}}

## Risk Level Guidelines
- "low": Minor changes, well-understood scope, minimal dependencies
- "medium": Moderate changes, some complexity, affects multiple components
- "high": Major changes, high complexity, critical systems, or unclear scope

Output only the JSON object:"""


def format_conversation(messages: list) -> str:
    """Format a list of chat messages into a conversation string.

    Args:
        messages: List of message objects with role, text, and timestamp attributes.

    Returns:
        Formatted conversation string with role labels and timestamps.
    """
    if not messages:
        return "(No messages in conversation)"

    lines = []
    for msg in messages:
        role_label = "[Host]" if msg.role == "host" else "[Engineer]"
        lines.append(f"{role_label}: {msg.text}")

    return "\n".join(lines)


def get_summary_prompt(messages: list) -> str:
    """Generate a complete summary prompt for the given messages.

    Args:
        messages: List of chat message objects.

    Returns:
        Complete prompt string ready for AI model input.
    """
    conversation = format_conversation(messages)
    return STRUCTURED_SUMMARY_PROMPT.format(conversation=conversation)


# Template for generating code prompts from decision summaries
CODE_PROMPT_TEMPLATE = """You are a senior software engineer tasked with implementing code changes.

## Problem Statement
{problem_statement}

## Proposed Solution
{proposed_solution}

## Target Components
{affected_components}

## Risk Level
{risk_level}

{context_section}
## Task
Based on the above information, implement the necessary code changes. Your output should be a unified diff format that can be applied to the codebase.

### Requirements:
1. Follow existing code patterns and conventions in the target components
2. Include appropriate error handling
3. Add or update tests if applicable
4. Ensure backward compatibility where possible
5. Document any breaking changes

### Output Format:
Provide your changes as unified diff patches that can be applied with `git apply` or similar tools. Each file change should be clearly marked with the file path.

Begin implementation:"""


def get_code_prompt(
    problem_statement: str,
    proposed_solution: str,
    affected_components: list,
    risk_level: str,
    context_snippet: str = None,
) -> str:
    """Generate a code prompt from a decision summary.

    Constructs a prompt that instructs a code generation model to produce
    unified diff output suitable for code proposal generation.

    Args:
        problem_statement: Description of the problem to solve.
        proposed_solution: The proposed solution or approach.
        affected_components: List of components/files that may be affected.
        risk_level: Risk assessment (low, medium, high).
        context_snippet: Optional code snippet for additional context.

    Returns:
        Complete code prompt string ready for code generation model input.
    """
    # Format affected components as a bulleted list
    if affected_components:
        components_str = "\n".join(f"- {comp}" for comp in affected_components)
    else:
        components_str = "- (No specific components identified)"

    # Format context section if provided
    if context_snippet:
        context_section = f"""## Context
The following code snippet provides relevant context:

```
{context_snippet}
```

"""
    else:
        context_section = ""

    return CODE_PROMPT_TEMPLATE.format(
        problem_statement=problem_statement or "No problem statement provided.",
        proposed_solution=proposed_solution or "No solution proposed.",
        affected_components=components_str,
        risk_level=risk_level or "unknown",
        context_section=context_section,
    )


# =============================================================================
# Selective Code Prompt Template for Multi-Type Summaries
# =============================================================================

SELECTIVE_CODE_PROMPT_SYSTEM = """You are a senior software engineer tasked with implementing changes based on structured engineering decisions.

You will receive:
- Only code-relevant discussion summaries
- Primary focus
- Impact scope

Your task:
- Convert decisions into actionable coding tasks
- Identify affected modules
- Specify required modifications
- Propose file-level changes
- Output a structured implementation plan
- If possible, include unified diff format suggestion

Output strictly JSON:
{
  "implementation_plan": {
    "affected_components": [...],
    "file_level_changes": [
      {
        "file": "...",
        "change_type": "modify|create|delete",
        "description": "..."
      }
    ],
    "tests_required": true|false,
    "migration_required": true|false,
    "risk_level": "low|medium|high"
  }
}"""

SELECTIVE_CODE_PROMPT_TEMPLATE = """## Primary Focus
{primary_focus}

## Impact Scope
{impact_scope}

## Code-Relevant Discussion Summaries

{summaries_section}

{context_section}## Task
Based on the above engineering decisions, provide a structured implementation plan.

### Requirements:
1. Analyze all code-relevant summaries and identify overlapping concerns
2. Consolidate affected components across all summaries
3. Specify file-level changes with clear descriptions
4. Determine if tests are required for the changes
5. Assess if any migrations (database, config, etc.) are needed
6. Provide an overall risk assessment

### Output Format:
Provide your response as valid JSON matching this schema:

{{
  "implementation_plan": {{
    "affected_components": ["list of affected modules/files"],
    "file_level_changes": [
      {{
        "file": "path/to/file.py",
        "change_type": "modify|create|delete",
        "description": "Description of what changes to make"
      }}
    ],
    "tests_required": true,
    "migration_required": false,
    "risk_level": "low|medium|high"
  }}
}}

Output only the JSON object:"""


def format_summaries_for_code_prompt(summaries: list) -> str:
    """Format a list of typed summaries into a section for the code prompt.

    Args:
        summaries: List of summary objects with discussion_type, topic,
                   core_problem, proposed_solution, affected_components, etc.

    Returns:
        Formatted string with all summaries structured for the prompt.
    """
    if not summaries:
        return "(No code-relevant summaries provided)"

    sections = []
    for i, summary in enumerate(summaries, 1):
        # Get attributes safely (works with both dicts and objects)
        if hasattr(summary, "__dict__"):
            disc_type = getattr(summary, "discussion_type", "unknown")
            topic = getattr(summary, "topic", "No topic")
            core_problem = getattr(summary, "core_problem", "")
            proposed_solution = getattr(summary, "proposed_solution", "")
            affected = getattr(summary, "affected_components", [])
            risk = getattr(summary, "risk_level", "low")
            next_steps = getattr(summary, "next_steps", [])
        else:
            disc_type = summary.get("discussion_type", "unknown")
            topic = summary.get("topic", "No topic")
            core_problem = summary.get("core_problem", "")
            proposed_solution = summary.get("proposed_solution", "")
            affected = summary.get("affected_components", [])
            risk = summary.get("risk_level", "low")
            next_steps = summary.get("next_steps", [])

        components_str = ", ".join(affected) if affected else "None specified"
        steps_str = "\n".join(f"  - {step}" for step in next_steps) if next_steps else "  - None specified"

        section = f"""### Summary {i}: {disc_type.upper()}
**Topic:** {topic}

**Problem:** {core_problem}

**Proposed Solution:** {proposed_solution}

**Affected Components:** {components_str}

**Risk Level:** {risk}

**Next Steps:**
{steps_str}
"""
        sections.append(section)

    return "\n---\n".join(sections)


def get_selective_code_prompt(
    primary_focus: str,
    impact_scope: str,
    summaries: list,
    context_snippet: str = None,
) -> str:
    """Generate a selective code prompt from multi-type summaries.

    Constructs a prompt that instructs a code generation model to produce
    a structured implementation plan from multiple code-relevant summaries.

    Args:
        primary_focus: The primary focus area of the implementation.
        impact_scope: The scope of impact (local, module, system, cross-system).
        summaries: List of code-relevant summary objects.
        context_snippet: Optional code snippet for additional context.

    Returns:
        Complete code prompt string ready for code generation model input.
    """
    # Format summaries section
    summaries_section = format_summaries_for_code_prompt(summaries)

    # Format context section if provided
    if context_snippet:
        context_section = f"""## Context
The following code snippet provides relevant context:

```
{context_snippet}
```

"""
    else:
        context_section = ""

    return SELECTIVE_CODE_PROMPT_TEMPLATE.format(
        primary_focus=primary_focus or "No primary focus specified",
        impact_scope=impact_scope or "local",
        summaries_section=summaries_section,
        context_section=context_section,
    )

