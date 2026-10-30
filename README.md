# Inngest Sandbox Playground

A per-user coding agent built on [Inngest](https://www.inngest.com) Sandboxes.

The agent's work from this demo lives in [Linell/stunning-pancake](https://github.com/Linell/stunning-pancake), one `user/<name>` branch per user.

> Sandboxes are experimental. The Python API (`inngest.experimental.sandbox`) can change without a major version bump.

<img width="2053" height="1070" alt="Screenshot 2026-10-30 at 9 52 02 AM" src="https://github.com/user-attachments/assets/0402968d-f006-41ae-8a25-6e9621ec9b48" />


## What it demonstrates

- **The function is the orchestrator.** `agent-session` runs a Claude tool-use loop inside an Inngest function. Each Claude call is a `step.run`, and the function holds `ANTHROPIC_API_KEY`. The sandbox never sees it.
- **The sandbox only runs code.** Each tool call (`list_files`, `read_file`, `write_file`, `run_command`) is a durable sandbox step. Step IDs are stable (`claude-<turn>`, `tool-<turn>.<i>-<name>`), so a replay rebuilds the conversation from recorded results instead of calling Claude or the sandbox again.
- **Git branches are the persistence layer.** Each user has a branch, `user/<name>`, in one repo. A session checks it out, lets Claude work, commits, and pushes. The next session starts where the last one stopped.
- **The deploy key never enters the agent's sandbox.** Every git command that needs the key runs in a fresh sandbox made for that one step. Checkout fetches the branch there and hands the agent's sandbox a git bundle; after the session, the agent's sandbox commits and writes a bundle, and a fresh sandbox checks it and pushes.
- **Personas and memory live in the repo.** A new branch is seeded with `AGENTS.md` from `personas/<name>.md`. `AGENTS.md` and the last 10 commit messages go into the system prompt, and Claude can add dated notes under its `## Memory` section. Claude's final reply becomes the commit message.
- **One session per user at a time.** Concurrency is limited to 1 per `event.data.user`, so two sessions never race to push the same branch. Different users run in parallel.

`demo-fanout` starts one session per user: the users in the event, or every persona in `personas/`.

## Prerequisites

- Python 3.10+ and [uv](https://docs.astral.sh/uv/).
- The Inngest Python SDK with sandbox support. `pyproject.toml` installs it from a sibling checkout at `../inngest-py`; change `[tool.uv.sources]` if yours lives elsewhere.
- Node.js, for the [Inngest Dev Server](https://www.inngest.com/docs/dev-server). Run `npx inngest-cli@latest login` with an account that has sandbox access; the Dev Server forwards sandbox requests to Inngest Cloud with those credentials.
- An Anthropic API key.
- A GitHub repo for the agent's work (it can be empty) and a deploy key with write access:

  ```sh
  ssh-keygen -t ed25519 -N "" -C sandbox-agent -f ~/.ssh/sandbox_agent_deploy
  ```

  Add `~/.ssh/sandbox_agent_deploy.pub` under the repo's **Settings → Deploy keys**, with **Allow write access** checked. Use a repo you don't mind the agent writing to (see [Design notes](#design-notes)).

## Environment

| Variable | Required | Description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | yes | Used by the function, never sent to the sandbox. |
| `AGENT_REPO` | yes | SSH URL of the agent's repo, e.g. `git@github.com:you/agent-workspace.git`. |
| `DEPLOY_KEY_PATH` | no | Private deploy key. Defaults to `~/.ssh/sandbox_agent_deploy`. |

## Run

```sh
uv run python app.py
npx inngest-cli@latest dev -u http://localhost:6967/api/inngest
```

Send an event from the Dev Server UI (http://localhost:8288), or:

```sh
curl -X POST http://localhost:8288/e/dev -d '{"name": "agent/demo.requested", "data": {"prompt": "...", "users": ["ada"]}}'
```

`users` is optional and defaults to every persona. To add a user, add `personas/<name>.md` with a `## Memory` section; the persona is only read when the user's branch is created.

## Design notes

- **Checkout and push use the direct client inside `step.run`.** A durable sandbox command that exits nonzero is a completed step: the exit code is a result, not an error. A failed push run that way would be recorded, and every retry would replay the failure. Calling `client.sandboxes_sync` inside `step.run` and raising on a nonzero exit fails the step, so Inngest retries it. A bundle or branch that retrying can't fix (exit code 3) fails the run instead. Each step moves the bundle between sandboxes itself and returns only a commit hash, so the bundle is never recorded.
- **The pushed bundle is untrusted.** The agent had a shell in the sandbox that made it. The push sandbox runs `git bundle verify`, accepts only `refs/heads/user/<name>`, fscks the objects, and pushes only a fast-forward of the remote branch, never with force. It uses a new bare repo with no hooks and ignores system and global git config. If the bundle's commit is the one the session started from, there's nothing to push. A retry after a push that succeeded finds the remote already at that commit and doesn't push again.
- **Pushing from a fresh sandbox, not the worker.** Git parses the agent's bundle there. A git bug triggered by a crafted bundle would reach a throwaway sandbox holding only the deploy key, not the process that holds `ANTHROPIC_API_KEY`, and the worker doesn't need git or ssh.
- **Keys are passed per command and never left on disk.** A snapshot keeps the sandbox's disk and any secrets delivered to it. The deploy key is passed in one git command's `environment`, written to a temp file, and deleted when the command exits. Those commands only run in the fresh git sandboxes, which are destroyed when the step ends. If the worker dies mid-step, nothing destroys that step's git sandbox (sandboxes have no TTL); a retry makes a new one rather than reusing a sandbox whose command may still be running. Delete leftover `git-*` sandboxes by hand. Python steps don't send their inputs to Inngest, so the key isn't recorded in step history either.
- **Sandboxes have unrestricted network egress.** Claude can run arbitrary commands, so anything reachable from the sandbox is reachable by the agent. Nothing secret is in the agent's sandbox: the deploy key only reaches sandboxes Claude never touches, and git hooks, git config, or programs Claude leaves behind don't run with it. What Claude can still do is push whatever it likes to its own `user/<name>` branch, as a fast-forward. Scope the key to a repo made for this anyway.
