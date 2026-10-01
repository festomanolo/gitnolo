# gitnolo

**Autonomous git for coding agents.** gitnolo watches Claude Code, Kiro, Antigravity and
other CLI agents across every repository on your machine. The moment an agent finishes a
task, it splits the work into up to 15 focused commits, pushes a branch, opens a pull
request, merges it, and files issues for problems the agent reported but did not fix.

Community edition: works on **public GitHub repositories** only.

```
             ▄██              ✻ Welcome to gitnolo
            ▄█▀██             Autonomous git for coding agents
           ██  ▀█▄
         ▄█▀    ██▄           repo       my-app on main
        ▄█▀      ██▄          agents     2 working · 1 waiting
 ▄▄    ██         ██    ▄▄▄   commits    up to 15 per change
 ▀▀█▄▄█▀           ██▄▄█▀▀    github     auto PR + merge
    ████▄▄       ▄▄███▀
  ▄█▀   ▀▀▀████▀▀▀▀  ██▄
 ▀▀                   ███
```

## Install

```bash
curl -fsSL https://festomanolo.com/GitNolo/install.sh | sh
gitnolo doctor
```

This needs macOS or Linux (on Windows, use WSL), Python 3.9+ and git. The installer
creates a private environment in `~/.gitnolo/app` and links `gitnolo` into
`~/.local/bin`. To uninstall: `rm -rf ~/.gitnolo/app ~/.local/bin/gitnolo`.

GitHub access is taken from `GITHUB_TOKEN`, `gh auth token`, or the credential
`git push` already uses. To set a token explicitly: `gitnolo config set github_token <token>`.

## Use

```bash
gitnolo watch -y        # live dashboard; commits, PRs and merges when agents finish
gitnolo commit          # do it now for the current repo
gitnolo agents --live   # which agent is doing what, where
```

## What it knows about your agents

gitnolo reads each agent's own session log, not just the process list. For every
session it knows whether the agent is:

- working
- waiting for your approval
- finished
- interrupted by you
- out of usage, along with the reset time
- failing on network, server or login errors
- stuck in a loop
- closed mid-task

Commits happen only when a task has really finished, no other agent is still
working in that repo, and the files have stopped changing. Work from an agent that
stopped early goes to a draft PR and is never merged until the agent completes.

## Commits

- **Up to 15 per change.** Commits are split from file groups down to individual
  lines, written in about a second, and verified against your files byte for byte.
  A small change gets fewer commits; there are never empty ones.
- **Messages name what changed.** Each message is a Conventional Commit naming the
  functions and classes it touches, for example `feat(api): add fetchUser()`.
- **Never touches your files.** Your working tree is untouched while agents keep
  editing.
- **Skips secrets.** `.env`, keys and certificates are never committed.

## GitLens in the terminal

```bash
gitnolo blame FILE[:START-END]     gitnolo history FILE[:START-END]
gitnolo graph                      gitnolo compare main feature/x
gitnolo branches                   gitnolo contributors
gitnolo hotspots                   gitnolo search QUERY --mode code
gitnolo show REV                   gitnolo timeline
gitnolo insights                   gitnolo stash | worktree | conflict | pr
```

## Supervise an agent

```bash
gitnolo supervise claude
```

gitnolo auto-approves the agent's confirmation prompts. Destructive commands such as
`rm -rf ~`, force pushes and `DROP TABLE` are never auto-approved; it alerts you
instead.

## Optional AI

gitnolo works fully offline: commit messages and issue detection are rule-based and
instant. For AI-written PR summaries and issue filtering, add a free OpenRouter key:

```bash
gitnolo config set openrouter_api_key sk-or-...
gitnolo ai --test
```

A built-in guard stays below OpenRouter's free-tier limits and falls back to the
rules when it reaches them. A local Ollama model also works:
`gitnolo config set ai_provider ollama`.

## Configuration

`gitnolo config` shows every setting, and `gitnolo config set <key> <value>` changes
one, for example `auto_merge false` or `settle_seconds 5`.

## Notes

- Commits are written with `git fast-import`: hooks do not run and commits are not
  signed.
- `gitnolo watch --plain` prints a line log instead of the dashboard, for running it
  as a background service.

MIT License · Made by [Festo K. Magembe](https://festomanolo.com) · [festomanolo.com/GitNolo](https://festomanolo.com/GitNolo)
