"""
Per-user coding agent on Inngest Sandboxes. See README.md.

    uv run python app.py
    npx inngest-cli@latest dev -u http://localhost:6967/api/inngest
"""

import base64
import os
import pathlib
import posixpath
import re
import shlex
import sys
import typing
import uuid

import anthropic
import flask
import inngest
import inngest.flask
from anthropic.types import (
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
)
from inngest.experimental.sandbox import (
    DurableSandboxSync,
    SandboxCommandResult,
    SandboxSync,
)

client = inngest.Inngest(app_id="sandbox-playground", is_production=False)
claude = anthropic.Anthropic()

MODEL = "claude-opus-5-5"
MAX_TURNS = 20
MAX_TOOL_OUTPUT_CHARS = 10_000

REPO = os.environ.get("AGENT_REPO") or sys.exit(
    "Set AGENT_REPO to the SSH URL of the agent's repo, e.g. git@github.com:you/agent-workspace.git"
)
DEPLOY_KEY = pathlib.Path(
    os.environ.get("DEPLOY_KEY_PATH", "~/.ssh/sandbox_agent_deploy")
).expanduser()
WORKDIR = "/work"
PERSONAS = pathlib.Path(__file__).parent / "personas"

BUNDLE = "/tmp/branch.bundle"

# Exit code for a branch or bundle that retrying won't fix.
REJECTED = 3

# Sandbox file transfers are limited to 100 MiB.
MAX_BUNDLE_BYTES = 100 * 1024 * 1024

NO_CHANGES = "no changes"


# Inngest functions


@client.create_function(
    fn_id="agent-session",
    trigger=inngest.TriggerEvent(event="agent/session.requested"),
    # One session per user at a time, so two runs never race to push.
    concurrency=[inngest.Concurrency(limit=1, key="event.data.user")],
    retries=2,
)
def agent_session(ctx: inngest.ContextSync) -> dict[str, typing.Any]:
    user = str(ctx.event.data["user"])
    prompt = str(ctx.event.data["prompt"])
    if not re.fullmatch(r"[A-Za-z0-9_-]+", user):
        raise inngest.NonRetriableError(f"invalid user {user!r}")
    branch = f"user/{user}"

    with ctx.group.sandbox("sandbox", vcpu=1, memory_mb=1024) as sandbox:
        base = ctx.step.run("checkout", checkout, sandbox.id, branch)
        if base is None:
            seed_persona(sandbox, user)
        summary, turns = run_agent(ctx, sandbox, prompt, read_context(sandbox))
        commit = commit_and_push(ctx, sandbox, branch, base, summary)

    return {
        "user": user,
        "branch": branch,
        "commit": commit,
        "turns": turns,
        "summary": summary,
    }


@client.create_function(
    fn_id="demo-fanout",
    trigger=inngest.TriggerEvent(event="agent/demo.requested"),
)
def demo_fanout(ctx: inngest.ContextSync) -> list[str]:
    prompt = ctx.event.data.get("prompt")
    if not prompt:
        raise inngest.NonRetriableError(
            'event.data.prompt is required: {"name": "agent/demo.requested", "data": {"prompt": "..."}}'
        )
    users = ctx.event.data.get("users") or persona_names()
    if not isinstance(users, list) or not all(isinstance(u, str) for u in users):
        raise inngest.NonRetriableError("event.data.users must be a list of names")

    return ctx.step.send_event(
        "start-sessions",
        [
            inngest.Event(
                name="agent/session.requested",
                data={"user": str(user), "prompt": prompt},
            )
            for user in users
        ],
    )


# Session steps


def checkout(sandbox_id: str, branch: str) -> str | None:
    """
    Check out the user's branch in WORKDIR. Returns the branch's commit, or
    None if the branch is new.
    """

    sandbox = get_sandbox(sandbox_id)
    with git_sandbox() as trusted:
        base = run_git(trusted, FETCH, branch) or None
        bundle = trusted.files.download(BUNDLE) if base else None
    if bundle is None:
        run_script(sandbox, INIT, {"BRANCH": branch})
        return None
    sandbox.files.upload(BUNDLE, bundle)
    run_script(sandbox, CLONE, {"BRANCH": branch})
    return base


def seed_persona(sandbox: DurableSandboxSync, user: str) -> None:
    persona = PERSONAS / f"{user}.md"
    if not persona.exists():
        raise inngest.NonRetriableError(f"no persona for {user}")
    sandbox.files.upload("seed-agents-md", f"{WORKDIR}/AGENTS.md", persona.read_text())


def read_context(sandbox: DurableSandboxSync) -> str:
    """AGENTS.md plus recent commit messages, for the system prompt."""

    return sandbox.commands.run("read-context", READ_CONTEXT, cwd=WORKDIR).stdout


def commit_and_push(
    ctx: inngest.ContextSync,
    sandbox: DurableSandboxSync,
    branch: str,
    base: str | None,
    summary: str,
) -> str:
    """Returns the pushed commit's short hash, or NO_CHANGES."""

    bundled = sandbox.commands.run(
        "commit",
        COMMIT,
        environment={"BRANCH": branch, "MESSAGE": commit_message(summary)},
        timeout=120_000,
    )
    if bundled.exit_code != 0:
        raise inngest.NonRetriableError(f"commit failed: {bundled.stderr}")
    if bundled.stdout.strip() == NO_CHANGES:
        return NO_CHANGES
    return ctx.step.run("push", push, sandbox.id, branch, base or "")


def push(sandbox_id: str, branch: str, base: str) -> str:
    """Returns the pushed commit's short hash, or NO_CHANGES."""

    # The bundle's bytes stay inside this step: its return value is recorded.
    bundle = get_sandbox(sandbox_id).files.download(BUNDLE)
    with git_sandbox() as trusted:
        trusted.files.upload(BUNDLE, bundle)
        return run_git(trusted, PUSH, branch, {"BASE": base})


def commit_message(summary: str) -> str:
    return next(iter(summary.strip().splitlines()), "agent session")[:200]


def persona_names() -> list[str]:
    return [p.stem for p in sorted(PERSONAS.glob("*.md"))]


# Agent loop


def run_agent(
    ctx: inngest.ContextSync, sandbox: DurableSandboxSync, prompt: str, context: str
) -> tuple[str, int]:
    """Run the tool-use loop. Returns (Claude's final text, turns taken)."""

    system: list[TextBlockParam] = [
        {"type": "text", "text": SYSTEM},
        {"type": "text", "text": context},
    ]
    messages: list[MessageParam] = [{"role": "user", "content": prompt}]

    for turn in range(MAX_TURNS):
        response = ctx.step.run(f"claude-{turn}", call_claude, system, messages)
        content = response["content"]
        stop_reason = response["stop_reason"]
        messages.append({"role": "assistant", "content": content})

        if stop_reason != "tool_use":
            return final_text(content, stop_reason), turn + 1

        messages.append(
            {"role": "user", "content": run_tool_calls(sandbox, turn, content)}
        )

    return f"stopped after {MAX_TURNS} turns", MAX_TURNS


def final_text(content: list[dict[str, typing.Any]], stop_reason: str) -> str:
    return next(
        (b["text"] for b in reversed(content) if b["type"] == "text"),
        f"stopped: {stop_reason}",
    )


def call_claude(
    system: list[TextBlockParam], messages: list[MessageParam]
) -> dict[str, typing.Any]:
    response = claude.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system,
        tools=TOOLS,
        messages=messages,
        output_config={"effort": "high"},
        cache_control={"type": "ephemeral"},
    )
    return response.model_dump(mode="json", exclude_none=True)


def run_tool_calls(
    sandbox: DurableSandboxSync, turn: int, content: list[dict[str, typing.Any]]
) -> list[ToolResultBlockParam]:
    results: list[ToolResultBlockParam] = []
    for i, block in enumerate(b for b in content if b["type"] == "tool_use"):
        output, is_error = run_tool(
            sandbox, f"tool-{turn}.{i}-{block['name']}", block["name"], block["input"]
        )
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": output,
                "is_error": is_error,
            }
        )
    return results


def run_tool(
    sandbox: DurableSandboxSync,
    step_id: str,
    name: str,
    args: dict[str, typing.Any],
) -> tuple[str, bool]:
    """Run one tool call as sandbox steps. Returns (output, is_error)."""

    if name == "list_files":
        path = shlex.quote(resolve(args.get("path")))
        result = sandbox.commands.run(
            step_id, f"find {path} -name .git -prune -o -print | sort | head -500"
        )
    elif name == "read_file":
        result = sandbox.commands.run(step_id, ["cat", "--", resolve(args["path"])])
    elif name == "write_file":
        path = resolve(args["path"])
        sandbox.commands.run(f"{step_id}:mkdir", ["mkdir", "-p", posixpath.dirname(path)])
        written = sandbox.files.upload(step_id, path, args["content"])
        return f"Wrote {written.bytes_written} bytes to {path}", False
    elif name == "run_command":
        result = sandbox.commands.run(
            step_id, args["command"], cwd=WORKDIR, timeout=120_000
        )
    else:
        return f"Unknown tool {name}", True

    return format_result(result)


def format_result(result: SandboxCommandResult) -> tuple[str, bool]:
    """Returns (output, is_error) for a tool result."""

    output = result.stdout + (f"\n[stderr]\n{result.stderr}" if result.stderr else "")
    if result.exit_code != 0:
        output += f"\n[exit code {result.exit_code}]"
    return clip(output), result.exit_code != 0


def resolve(path: str | None) -> str:
    return posixpath.normpath(posixpath.join(WORKDIR, path or "."))


def clip(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    dropped = len(text) - MAX_TOOL_OUTPUT_CHARS
    return text[:MAX_TOOL_OUTPUT_CHARS] + f"\n[truncated {dropped} characters]"


# Git helpers


def get_sandbox(sandbox_id: str) -> SandboxSync:
    sandbox = client.sandboxes_sync.get(sandbox_id)
    if sandbox is None:
        raise inngest.NonRetriableError("sandbox is gone")
    return sandbox


def git_sandbox() -> SandboxSync:
    """A fresh sandbox for git commands that need the deploy key."""

    return client.sandboxes_sync.create(
        name=f"git-{uuid.uuid4().hex}", vcpu=1, memory_mb=1024
    )


def run_git(
    sandbox: SandboxSync, script: str, branch: str, env: dict[str, str] | None = None
) -> str:
    return run_script(
        sandbox,
        script,
        {
            "DEPLOY_KEY_B64": base64.b64encode(DEPLOY_KEY.read_bytes()).decode(),
            "REPO": REPO,
            "BRANCH": branch,
            **(env or {}),
        },
    )


def run_script(sandbox: SandboxSync, script: str, env: dict[str, str]) -> str:
    """
    Run with the direct client, not as a durable command, so a nonzero exit
    fails the calling step and it retries.
    """

    result = sandbox.commands.run(script, environment=env, timeout=120_000)
    if result.exit_code == REJECTED:
        raise inngest.NonRetriableError(result.stderr.strip())
    if result.exit_code != 0:
        raise Exception(f"git failed: {result.stderr}")
    return result.stdout.strip()


# Claude prompt and tools


SYSTEM = f"""You are a coding agent working in a git checkout at {WORKDIR}, \
inside a sandbox, on behalf of one user. The repository is that user's \
workspace and persists between sessions: whatever you leave in it, they and \
you will see next time. Use the tools to inspect and change files and to run \
commands. When you're done, reply with a one-line summary of what you changed; \
it becomes the commit message.

AGENTS.md describes the user and how they want you to work. Follow it. If you \
learn something worth remembering for future sessions, add a short dated note \
under its Memory section."""

READ_CONTEXT = "cat AGENTS.md; echo; echo '## Recent sessions'; git log --oneline -10 2>/dev/null || echo none"

TOOLS: list[ToolParam] = [
    {
        "name": "list_files",
        "description": f"List files under a directory (default {WORKDIR}), excluding .git.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file. Relative paths are relative to the repository root.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": "Create or replace a file with the given content, creating parent directories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the repository root, such as running tests. Times out after 2 minutes.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
]


# Git scripts


CHECK_BUNDLE_SIZE = f"""
[ $(wc -c < {BUNDLE}) -le {MAX_BUNDLE_BYTES} ] || {{ echo "bundle is over 100 MiB" >&2; exit {REJECTED}; }}
"""

# Only runs in a fresh sandbox the agent never touches. The key exists only
# for the length of one command, so no snapshot can keep it.
GIT_WITH_KEY = f"""
set -e
key=$(mktemp)
trap 'rm -f "$key"' EXIT
echo "$DEPLOY_KEY_B64" | base64 -d > "$key"
chmod 600 "$key"
export GIT_SSH_COMMAND="ssh -i $key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
reject() {{ echo "$1" >&2; exit {REJECTED}; }}
git init -q --bare --template= /repo
cd /repo
git config core.hooksPath /dev/null
"""

# Prints the branch's commit and writes it to BUNDLE, or prints nothing if the
# branch is new. A failed ls-remote fails the script, so it never looks new.
FETCH = (
    GIT_WITH_KEY
    + f"""
refs=$(git ls-remote "$REPO" "refs/heads/$BRANCH")
[ -n "$refs" ] || exit 0
git fetch -q "$REPO" "refs/heads/$BRANCH:refs/heads/$BRANCH"
git bundle create {BUNDLE} "refs/heads/$BRANCH"
"""
    + CHECK_BUNDLE_SIZE
    + """
git rev-parse "refs/heads/$BRANCH"
"""
)

# BUNDLE came from the agent's sandbox, so it's untrusted: one ref, verified,
# fsck'd, and only pushed as a fast-forward. BASE is the commit the session
# started from, empty for a new branch.
PUSH = (
    GIT_WITH_KEY
    + f"""
problems=$(git bundle verify {BUNDLE} 2>&1) || reject "invalid bundle: $problems"
heads=$(git bundle list-heads {BUNDLE})
tip=${{heads%% *}}
case $tip in ""|*[!0-9a-f]*) reject "invalid bundle" ;; esac
[ "$heads" = "$tip refs/heads/$BRANCH" ] || reject "bundle must contain only refs/heads/$BRANCH"
if [ "$tip" = "$BASE" ]; then
    echo "{NO_CHANGES}"
    exit 0
fi
git fetch -q {BUNDLE} "refs/heads/$BRANCH:refs/agent/tip" || reject "invalid bundle"
# Older git ignores fetch.fsckObjects for bundles.
problems=$(git fsck --strict --no-dangling 2>&1) || reject "bundle failed fsck: $problems"
refs=$(git ls-remote "$REPO" "refs/heads/$BRANCH")
remote=$(echo "$refs" | cut -f1)
if [ -n "$remote" ] && [ "$remote" != "$tip" ]; then
    git fetch -q "$REPO" "refs/heads/$BRANCH:refs/remote/tip"
    git merge-base --is-ancestor "$remote" "$tip" || reject "not a fast-forward of $remote"
fi
# A retry after a push that succeeded finds the remote already at tip.
[ "$remote" = "$tip" ] || git push -q "$REPO" "$tip:refs/heads/$BRANCH"
git rev-parse --short "$tip"
"""
)

GIT_IDENTITY = """
git config user.name "Sandbox Agent"
git config user.email "sandbox-agent@example.com"
"""

# Runs in the agent's sandbox, without the key.
INIT = (
    f"""
set -e
rm -rf {WORKDIR}
git init -q {WORKDIR}
cd {WORKDIR}
git symbolic-ref HEAD "refs/heads/$BRANCH"
"""
    + GIT_IDENTITY
)

CLONE = (
    f"""
set -e
rm -rf {WORKDIR}
git clone -q -b "$BRANCH" {BUNDLE} {WORKDIR}
rm -f {BUNDLE}
cd {WORKDIR}
git remote remove origin
"""
    + GIT_IDENTITY
)

# Runs in the agent's sandbox, without the key, and only produces a bundle for
# PUSH to check. Prints NO_CHANGES if there's nothing to bundle.
COMMIT = (
    f"""
set -e
cd {WORKDIR}
git add -A
git diff --cached --quiet || git commit -q -m "$MESSAGE"
if ! git rev-parse -q --verify HEAD >/dev/null; then
    echo "{NO_CHANGES}"
    exit 0
fi
git update-ref "refs/heads/$BRANCH" HEAD
rm -f {BUNDLE}
git bundle create {BUNDLE} "refs/heads/$BRANCH"
"""
    + CHECK_BUNDLE_SIZE
)


app = flask.Flask(__name__)
inngest.flask.serve(app, client, [agent_session, demo_fanout])

if __name__ == "__main__":
    app.run(port=6967)
