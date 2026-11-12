"""How the Director starts Claude Code for HEAD, the Reviewer, the proposal Builder and research.

Every one of these roles only reads: the Director itself writes the files and runs the tests. Codex is started in
its read-only sandbox; without the arguments below Claude Code started with whatever tools and permissions the
operator's own settings allow, so a Claude HEAD could run commands in the worktree (it did, 2026-11-12) and reach
the operator's connected apps.

  --tools Read,Grep,Glob      only these built-in tools exist in the session (no Bash, Edit, Write, sub-agents)
  --disallowedTools mcp__*    --tools does not cover MCP tools; this removes the operator's connectors
  --permission-prompts none   nobody is there to answer a prompt: deny instead of waiting (Claude Code >= 2.1.259)
  --setting-sources user      ignore the settings files of the project being worked on: a Builder could write
                              .claude/settings.json with a hook, and a hook runs a command whatever the tools are

config.yaml `claude_cli.read_only_args` replaces the list (an empty list restores the old behavior).
"""
from typing import List

DEFAULT_READ_ONLY_ARGS = ["--tools", "Read,Grep,Glob", "--disallowedTools", "mcp__*",
                          "--permission-prompts", "none", "--setting-sources", "user"]


def read_only_args() -> List[str]:
    try:
        from director import load_config
        configured = ((load_config() or {}).get("claude_cli") or {}).get("read_only_args")
    except Exception:
        configured = None
    if isinstance(configured, (list, tuple)) and all(isinstance(a, str) for a in configured):
        return list(configured)
    return list(DEFAULT_READ_ONLY_ARGS)


def describe(args: List[str]) -> str:
    return (" " + " ".join(args)) if args else ""


def old_cli_hint(exit_code, output: str) -> str:
    """'' unless the CLI rejected one of the arguments above; then a message that names the fix."""
    text = (output or "").lower()
    if exit_code and "unknown option" in text and any(a.lstrip("-").lower() in text for a in DEFAULT_READ_ONLY_ARGS[::2]):
        return ("CONFIG_ERROR: this Claude Code CLI does not know the read-only arguments the Director passes "
                "(needs Claude Code 2.1.259 or later). Update Claude Code, or set claude_cli.read_only_args in "
                "config.yaml to arguments your version accepts. ")
    return ""
