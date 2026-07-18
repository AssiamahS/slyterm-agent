# slyterm

A tiny Claude-Code-style terminal agent that costs $0 and needs **no sign-in and no API key**.

It runs on the free [GitHub Models](https://docs.github.com/en/github-models) inference API and authenticates with the GitHub login you already have via the `gh` CLI. Single Python file, stdlib only.

## Why it exists

Free-tier GitHub Models caps every request at ~8K input tokens. Full-size agents (Claude Code, opencode, gemini-cli) ship system prompts and tool schemas bigger than that cap, so they can't run on it. slyterm keeps the whole agent small enough to fit.

## What it can do

- run shell commands and read/write files (agent loop: act → observe → repeat)
- search the web (DuckDuckGo) and fetch pages
- ride through free-tier rate limits automatically (gpt-4.1, falls back to gpt-4o-mini)

## Install

```sh
# needs: python3, curl, and a logged-in gh CLI (gh auth login)
git clone https://github.com/AssiamahS/slyterm-agent && cd slyterm-agent
mkdir -p ~/.local/bin
printf '#!/bin/zsh\nexport GITHUB_TOKEN=${GITHUB_TOKEN:-$(gh auth token)}\nexec python3 %s/slyterm.py "$@"\n' "$PWD" > ~/.local/bin/slyterm
chmod +x ~/.local/bin/slyterm
```

## Use

```sh
slyterm                          # interactive
slyterm -p "fix the failing test in this repo"   # one-shot
```

## Limits

Free tier is roughly 50 requests/day on gpt-4.1 and 150/day on gpt-4o-mini, 8K tokens in / 4K out per request, resets midnight UTC. Each agent step is one request, so a complex task burns 5–15. Good for real daily use, not for leaving an agent grinding all night.
