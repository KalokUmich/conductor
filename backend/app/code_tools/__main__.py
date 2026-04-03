"""CLI entry point for code tools.

Usage:
    python -m app.code_tools <tool_name> <workspace> '<json_params>'
    echo '{"pattern": "foo"}' | python -m app.code_tools grep /path/to/ws

Exit codes: 0 on success, 1 on error (JSON still written to stdout).
"""

import json
import logging
import sys

# Suppress all logging to stderr so only JSON reaches stdout.
logging.disable(logging.CRITICAL)


def main() -> None:
    args = sys.argv[1:]

    if not args:
        print(  # CLI output: intentional print — stdout is parsed as JSON by the TS extension
            json.dumps(
                {
                    "tool_name": "",
                    "success": False,
                    "error": "Usage: python -m app.code_tools <tool_name> <workspace> '[json_params]'",
                }
            ),
            flush=True,
        )
        sys.exit(1)

    tool_name = args[0]

    # Special command: list available tools.
    if tool_name == "list":
        from app.code_tools.tools import TOOL_REGISTRY

        print(json.dumps(sorted(TOOL_REGISTRY.keys())), flush=True)  # CLI output: intentional print
        sys.exit(0)

    if len(args) < 2:
        print(  # CLI output: intentional print
            json.dumps({"tool_name": tool_name, "success": False, "error": "Missing required argument: workspace"}),
            flush=True,
        )
        sys.exit(1)

    workspace = args[1]

    # Read params from arg 3 or stdin.
    if len(args) >= 3:
        raw_params = args[2]
    else:
        raw_params = sys.stdin.read()

    try:
        params = json.loads(raw_params) if raw_params.strip() else {}
    except json.JSONDecodeError as exc:
        print(  # CLI output: intentional print
            json.dumps({"tool_name": tool_name, "success": False, "error": f"Invalid JSON params: {exc}"}),
            flush=True,
        )
        sys.exit(1)

    from app.code_tools.tools import execute_tool

    result = execute_tool(tool_name, workspace, params)
    print(json.dumps(result.model_dump()), flush=True)  # CLI output: intentional print — TS extension reads stdout
    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
