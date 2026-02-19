"""Prompt templates for AI providers.

This module contains shared prompt templates used by AI providers
for various tasks like summarization, classification, and code generation.

All prompts follow Anthropic prompt engineering best practices:
- XML tags for structured data separation
- Specific role definitions
- Output schema blocks
- Guardrails for edge cases
"""
from typing import List, Literal, Optional

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

CLASSIFICATION_PROMPT = """You are a software engineering discussion classifier. Analyze a team conversation and assign it to exactly one category.

<conversation>
{conversation}
</conversation>

<categories>
Each category is defined by its PRIMARY SIGNAL — the dominant characteristic that separates it from the others:

1. **debugging** — A specific error, exception, crash, or unexpected behavior is being investigated or fixed.
   Primary signal: error messages, stack traces, "it's broken", "why is X happening", root cause analysis.

2. **api_design** — The team is deciding WHAT an API should look like: endpoint paths, HTTP methods, request/response schemas, versioning, contracts between services.
   Primary signal: endpoint paths, payload structure, REST/GraphQL semantics, API contracts.
   Note: Use this even if implementation is also discussed, as long as the API contract is the focus.

3. **architecture** — The team is deciding HOW to structure or organize the system: component relationships, data flow, patterns, infrastructure, service boundaries, technology choices.
   Primary signal: "should we use X or Y", service decomposition, database design, scalability decisions.

4. **product_flow** — The team is discussing user-facing behavior: what a feature should do from the user's perspective, UX decisions, acceptance criteria, user journeys, screen behavior.
   Primary signal: "the user should be able to", "when they click X", feature requirements, UI/UX decisions.

5. **innovation** — Exploring a new idea, technology, or approach where no clear implementation path exists yet. Speculative, research-oriented.
   Primary signal: "what if we", "I wonder if", proof-of-concept, unknowns, research, experimentation.

6. **code_change** — The team has already agreed on WHAT to build and is discussing the HOW: specific file/function changes, refactoring a known component, implementation approach for a well-defined task.
   Primary signal: concrete file or function names, clear task scope with agreed-upon direction, refactoring.
   Note: Do NOT use for bug investigation (→ debugging), API contract decisions (→ api_design), or structural design (→ architecture).

7. **general** — The conversation does not fit any category above, is too short or vague to classify, or covers multiple unrelated topics equally.
</categories>

<decision_guide>
Apply in this priority order — use the FIRST category whose primary signal is clearly present:
1. Is there a specific bug, error, or unexpected behavior being investigated? → debugging
2. Is the focus on what an API contract or endpoint should look like? → api_design
3. Is the focus on how to structure or organize components/systems? → architecture
4. Is the focus on user-facing behavior or feature requirements? → product_flow
5. Is this exploring an unproven idea with significant unknowns? → innovation
6. Is the team implementing a well-defined, already-agreed-upon change? → code_change
7. Otherwise → general
</decision_guide>

Output ONLY valid JSON with no markdown formatting, no code blocks, and no additional explanation.

<output_schema>
{{
  "discussion_type": "one of: api_design, product_flow, code_change, architecture, innovation, debugging, general",
  "confidence": 0.0 to 1.0
}}
</output_schema>

<examples>
Conversation: "The /users endpoint is returning 500 when email is null. Stack trace points to validate_user(). We need to add a null check."
Output: {{"discussion_type": "debugging", "confidence": 0.95}}

Conversation: "Should GET /users return all fields or just public ones? And should we use offset or cursor-based pagination?"
Output: {{"discussion_type": "api_design", "confidence": 0.91}}

Conversation: "I think we should split auth and billing into separate services. They have no reason to share a database."
Output: {{"discussion_type": "architecture", "confidence": 0.88}}

Conversation: "When the user clicks Save, show a loading spinner and disable the button until the API responds."
Output: {{"discussion_type": "product_flow", "confidence": 0.87}}

Conversation: "What if we replaced Anthropic with a local LLM? Let's prototype it and see if latency is acceptable."
Output: {{"discussion_type": "innovation", "confidence": 0.84}}

Conversation: "Let's refactor ChatManager to extract the connection pool. We know the files: chat/manager.py and chat/pool.py."
Output: {{"discussion_type": "code_change", "confidence": 0.90}}

Conversation: "We need to add pagination to the users list and also update the API docs."
Output: {{"discussion_type": "api_design", "confidence": 0.78}}
</examples>

Output only the JSON object:"""


# =============================================================================
# Stage 2: Specialized Summary Prompts by Discussion Type
# =============================================================================

# Base template structure for all specialized prompts
_SUMMARY_BASE = """You are a software engineering decision summarizer. Your task is to extract structured information from a team conversation.

<conversation>
{conversation}
</conversation>

<context>
This conversation has been classified as: {discussion_type}
</context>

<instructions>
{specialized_instructions}

If the conversation lacks sufficient information for a field:
- Use an empty string "" for text fields where no information is available
- Use false for requires_code_change if unclear
- Use an empty array [] for lists with no items
- Use "low" for risk_level if assessment is not possible
- For topic, provide "No clear topic identified" if truly unclear

Impact Scope Guidelines:
- "local": Changes affect only a single function or class
- "module": Changes affect multiple files within one module/package
- "system": Changes affect multiple modules or the entire application
- "cross-system": Changes affect multiple systems or external integrations

Risk Level Guidelines:
- "low": Minor changes, well-understood scope, minimal dependencies
- "medium": Moderate changes, some complexity, affects multiple components
- "high": Major changes, high complexity, critical systems, or unclear scope

Output ONLY valid JSON with no markdown formatting, no code blocks, and no additional explanation.
</instructions>

<output_schema>
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
</output_schema>

Output only the JSON object:"""

# Specialized instructions for each discussion type
SPECIALIZED_INSTRUCTIONS = {
    "api_design": """Pay special attention to:
1. Endpoint paths and HTTP methods discussed
2. Request/response payload structures
3. Authentication and authorization requirements
4. Versioning and backward compatibility concerns
5. Rate limiting or performance considerations

For requires_code_change: Set to TRUE if new endpoints need to be created or existing ones modified.
For impact_scope: Consider how many services/clients will be affected by API changes.""",

    "product_flow": """Pay special attention to:
1. User journey and interaction patterns
2. Feature requirements and acceptance criteria
3. Edge cases and error handling from user perspective
4. Integration with existing features
5. Data requirements and state management

For requires_code_change: Set to TRUE if UI/UX changes or new features are needed.
For impact_scope: Consider frontend, backend, and any data model changes.""",

    "code_change": """Pay special attention to:
1. Specific files, functions, or classes mentioned
2. Implementation approach and patterns
3. Dependencies and imports affected
4. Test coverage requirements
5. Performance implications

For requires_code_change: Almost always TRUE for this discussion type.
For impact_scope: Assess based on the number of files and modules touched.""",

    "architecture": """Pay special attention to:
1. System components and their interactions
2. Data flow and storage decisions
3. Scalability and performance considerations
4. Technology choices and trade-offs
5. Migration or transition plans

For requires_code_change: Set to TRUE if architectural changes require implementation.
For impact_scope: Usually "system" or "cross-system" for architecture discussions.""",

    "innovation": """Pay special attention to:
1. Novel approaches or technologies being explored
2. Proof of concept requirements
3. Risks and unknowns
4. Success criteria and metrics
5. Resource and timeline estimates

For requires_code_change: Set to TRUE if prototyping or experimentation code is needed.
For impact_scope: Consider if this is isolated experimentation or broader integration.""",

    "debugging": """Pay special attention to:
1. Error messages, stack traces, or symptoms described
2. Steps to reproduce the issue
3. Root cause analysis and findings
4. Proposed fix or workaround
5. Prevention measures for the future

For requires_code_change: Set to TRUE if a fix needs to be implemented.
For impact_scope: Assess based on whether the bug is localized or systemic.""",

    "general": """Extract the most relevant information:
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
STRUCTURED_SUMMARY_PROMPT = """You are a software engineering decision summarizer.

Your task is to analyze the following conversation between a host and an engineer, then extract key information about problems discussed, decisions made, and whether code changes are required.

<conversation>
{conversation}
</conversation>

<instructions>
1. Identify the main topic or subject being discussed
2. Extract the core problem or challenge being addressed
3. Summarize the proposed solution or approach (if any)
4. Determine if code changes are required based on the discussion
5. List any components, files, or systems that would be affected
6. Assess the risk level based on scope and complexity
7. Extract clear action items or next steps

If the conversation lacks sufficient information for a field:
- Use an empty string "" for text fields where no information is available
- Use false for requires_code_change if unclear
- Use an empty array [] for lists with no items
- Use "low" for risk_level if assessment is not possible
- For topic, provide "No clear topic identified" if truly unclear

Output ONLY valid JSON with no markdown formatting, no code blocks, and no additional explanation.
</instructions>

<output_schema>
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
</output_schema>

<risk_guidelines>
- "low": Minor changes, well-understood scope, minimal dependencies
- "medium": Moderate changes, some complexity, affects multiple components
- "high": Major changes, high complexity, critical systems, or unclear scope
</risk_guidelines>

Output only the JSON object:"""


def format_conversation(messages: list) -> str:
    """Format a list of chat messages into XML-tagged conversation string.

    Args:
        messages: List of message objects with role, text, and timestamp attributes.

    Returns:
        Formatted conversation string with XML message tags.
    """
    if not messages:
        return "(No messages in conversation)"

    lines = []
    for msg in messages:
        role = "host" if msg.role == "host" else "engineer"
        lines.append(f'<message role="{role}">{msg.text}</message>')

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


# =============================================================================
# Code Prompt Template
# =============================================================================

CODE_PROMPT_TEMPLATE = """You are a senior software engineer tasked with implementing code changes.

<problem>
{problem_statement}
</problem>

<solution>
{proposed_solution}
</solution>

<target_components>
{affected_components}
</target_components>

<risk_level>{risk_level}</risk_level>

{context_section}{policy_section}{style_section}<instructions>
Based on the above information, implement the necessary code changes. Your output should be a unified diff format that can be applied to the codebase.

Requirements:
1. Follow existing code patterns and conventions in the target components
2. Include appropriate error handling
3. Add or update tests if applicable
4. Ensure backward compatibility where possible
5. Document any breaking changes

Output Format:
Provide your changes as unified diff patches that can be applied with `git apply` or similar tools. Each file change should be clearly marked with the file path.
</instructions>

Begin implementation:"""


def get_code_prompt(
    problem_statement: str,
    proposed_solution: str,
    affected_components: list,
    risk_level: str,
    context_snippet: str = None,
    policy_constraints: str = None,
    style_guidelines: str = None,
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
        policy_constraints: Optional policy constraints string.
        style_guidelines: Optional code style guidelines string.

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
        context_section = f"""<context>
The following code snippet provides relevant context:

```
{context_snippet}
```
</context>

"""
    else:
        context_section = ""

    # Format policy section if provided
    if policy_constraints:
        policy_section = f"""<policy_constraints>
{policy_constraints}
</policy_constraints>

"""
    else:
        policy_section = ""

    # Format style section if provided
    if style_guidelines:
        style_section = f"""<code_style>
{style_guidelines}
</code_style>

"""
    else:
        style_section = ""

    return CODE_PROMPT_TEMPLATE.format(
        problem_statement=problem_statement or "No problem statement provided.",
        proposed_solution=proposed_solution or "No solution proposed.",
        affected_components=components_str,
        risk_level=risk_level or "unknown",
        context_section=context_section,
        policy_section=policy_section,
        style_section=style_section,
    )


# =============================================================================
# Selective Code Prompt Template for Multi-Type Summaries
# =============================================================================

SELECTIVE_CODE_PROMPT_SYSTEM = """You are a senior software engineer tasked with implementing changes based on structured engineering decisions.

You will receive:
- Only code-relevant discussion summaries
- Primary focus
- Impact scope

{policy_section}{style_section}<instructions>
Your task:
- Convert decisions into actionable coding tasks
- Identify affected modules
- Specify required modifications
- Propose file-level changes
- Output a structured implementation plan
- If possible, include unified diff format suggestion

Output strictly JSON matching the schema below.
</instructions>

<output_schema>
{{
  "implementation_plan": {{
    "affected_components": [...],
    "file_level_changes": [
      {{
        "file": "...",
        "change_type": "modify|create|delete",
        "description": "..."
      }}
    ],
    "tests_required": true|false,
    "migration_required": true|false,
    "risk_level": "low|medium|high"
  }}
}}
</output_schema>"""

SELECTIVE_CODE_PROMPT_TEMPLATE = """<primary_focus>{primary_focus}</primary_focus>

<impact_scope>{impact_scope}</impact_scope>

<summaries>
{summaries_section}
</summaries>

{context_section}<instructions>
Based on the above engineering decisions, provide a structured implementation plan.

Requirements:
1. Analyze all code-relevant summaries and identify overlapping concerns
2. Consolidate affected components across all summaries
3. Specify file-level changes with clear descriptions
4. Determine if tests are required for the changes
5. Assess if any migrations (database, config, etc.) are needed
6. Provide an overall risk assessment

Output Format:
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

Output only the JSON object.
</instructions>"""


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

        section = f"""<summary index="{i}" type="{disc_type}">
Topic: {topic}
Problem: {core_problem}
Proposed Solution: {proposed_solution}
Affected Components: {components_str}
Risk Level: {risk}
Next Steps:
{steps_str}
</summary>"""
        sections.append(section)

    return "\n".join(sections)


def get_selective_code_prompt(
    primary_focus: str,
    impact_scope: str,
    summaries: list,
    context_snippet: str = None,
    policy_constraints: str = None,
    style_guidelines: str = None,
) -> str:
    """Generate a selective code prompt from multi-type summaries.

    Constructs a prompt that instructs a code generation model to produce
    a structured implementation plan from multiple code-relevant summaries.

    Args:
        primary_focus: The primary focus area of the implementation.
        impact_scope: The scope of impact (local, module, system, cross-system).
        summaries: List of code-relevant summary objects.
        context_snippet: Optional code snippet for additional context.
        policy_constraints: Optional policy constraints string.
        style_guidelines: Optional code style guidelines string.

    Returns:
        Complete code prompt string ready for code generation model input.
    """
    # Format summaries section
    summaries_section = format_summaries_for_code_prompt(summaries)

    # Format context section if provided
    if context_snippet:
        context_section = f"""<context>
The following code snippet provides relevant context:

```
{context_snippet}
```
</context>

"""
    else:
        context_section = ""

    return SELECTIVE_CODE_PROMPT_TEMPLATE.format(
        primary_focus=primary_focus or "No primary focus specified",
        impact_scope=impact_scope or "local",
        summaries_section=summaries_section,
        context_section=context_section,
    )


# =============================================================================
# Stage 4: Code Relevant Items Extraction Prompt
# =============================================================================

CODE_RELEVANT_ITEMS_PROMPT = """You are a software engineering task decomposer. Your job is to extract discrete, actionable implementation tasks from a discussion summary.

<summary>
Discussion Type: {discussion_type}
Topic: {topic}
Core Problem: {core_problem}
Proposed Solution: {proposed_solution}
Affected Components: {affected_components}
Next Steps: {next_steps}
</summary>

<instructions>
Extract 1 to 5 discrete implementation items from the summary above. Each item should be a single, focused coding task that a developer could pick up independently.

Rules:
- Each item must have a clear, imperative title (e.g., "Add pagination to /users endpoint")
- The "type" must be one of: api_design, code_change, product_flow, architecture, debugging
  - api_design: the item is about creating or modifying an API endpoint or contract
  - debugging: the item is a specific bug fix
  - architecture: the item restructures how components relate
  - product_flow: the item changes user-visible behavior or UI
  - code_change: the item is a concrete implementation not covered by the above
- "targets" should list specific file paths or component names when mentioned, otherwise use descriptive component names
- "risk_level" must be one of: low, medium, high
- If the summary describes a single focused change, return exactly 1 item
- If the summary describes multiple distinct changes, split them into separate items (up to 5)
- Assign sequential IDs: "item-1", "item-2", etc.

Output ONLY a valid JSON array with no markdown formatting, no code blocks, and no additional explanation.
</instructions>

<output_schema>
[
  {{
    "id": "item-1",
    "type": "code_change",
    "title": "Short imperative title",
    "problem": "Specific problem this item addresses",
    "proposed_change": "What code change to make",
    "targets": ["file/path.py", "component_name"],
    "risk_level": "low"
  }}
]
</output_schema>

<example>
Input summary:
Discussion Type: api_design
Topic: Add user authentication with JWT
Core Problem: No secure authentication mechanism exists
Proposed Solution: Implement JWT-based auth with login/logout endpoints and middleware
Affected Components: auth/login.py, auth/middleware.py, api/routes.py
Next Steps: Create JWT utility, Add login endpoint, Add auth middleware

Output:
[
  {{
    "id": "item-1",
    "type": "api_design",
    "title": "Create JWT token utility and login endpoint",
    "problem": "No authentication endpoint exists for users to obtain tokens",
    "proposed_change": "Add POST /auth/login endpoint that validates credentials and returns JWT tokens",
    "targets": ["auth/login.py", "api/routes.py"],
    "risk_level": "medium"
  }},
  {{
    "id": "item-2",
    "type": "code_change",
    "title": "Add JWT authentication middleware",
    "problem": "API routes are not protected by authentication",
    "proposed_change": "Create middleware that validates JWT tokens on protected routes and rejects unauthorized requests",
    "targets": ["auth/middleware.py"],
    "risk_level": "medium"
  }}
]
</example>

Output only the JSON array:"""


def get_code_relevant_items_prompt(summary) -> str:
    """Generate a prompt for extracting code-relevant items from a summary.

    Args:
        summary: A PipelineSummary or dict-like object with summary fields.

    Returns:
        Complete prompt string ready for AI model input.
    """
    # Support both dataclass and dict access
    if hasattr(summary, "discussion_type"):
        discussion_type = summary.discussion_type
        topic = summary.topic
        core_problem = summary.core_problem
        proposed_solution = summary.proposed_solution
        affected_components = summary.affected_components
        next_steps = summary.next_steps
    else:
        discussion_type = summary.get("discussion_type", "general")
        topic = summary.get("topic", "")
        core_problem = summary.get("core_problem", "")
        proposed_solution = summary.get("proposed_solution", "")
        affected_components = summary.get("affected_components", [])
        next_steps = summary.get("next_steps", [])

    components_str = ", ".join(affected_components) if affected_components else "None specified"
    steps_str = ", ".join(next_steps) if next_steps else "None specified"

    return CODE_RELEVANT_ITEMS_PROMPT.format(
        discussion_type=discussion_type,
        topic=topic,
        core_problem=core_problem,
        proposed_solution=proposed_solution,
        affected_components=components_str,
        next_steps=steps_str,
    )


def format_policy_constraints(
    max_files: int,
    max_lines_changed: int,
    forbidden_paths: tuple = (),
) -> str:
    """Format policy constraints into a human-readable string.

    Args:
        max_files: Maximum number of files allowed.
        max_lines_changed: Maximum total lines changed allowed.
        forbidden_paths: Tuple of forbidden path prefixes.

    Returns:
        Formatted policy constraints string.
    """
    lines = [
        f"- Maximum files that may be changed: {max_files}",
        f"- Maximum total lines changed: {max_lines_changed}",
    ]
    if forbidden_paths:
        paths_str = ", ".join(forbidden_paths)
        lines.append(f"- Do NOT modify files under these paths: {paths_str}")
    return "\n".join(lines)
