# Use Fledge from an AI agent

Fledge ships an [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills) that
teaches an LLM agent the inspect → fix → check → prepare loop, how to read the
compact and JSON reports, what each exit code means, and which safety rules apply.
The skill only describes the installed `fledge` command; it adds no network access
or permissions.

## Install the skill

From a source checkout or extracted source distribution, copy the skill folder into
the agent's skill directory. For Claude Code:

```sh
# For all your projects:
mkdir -p ~/.claude/skills && cp -R skills/fledge ~/.claude/skills/
# Or only for one paper repository:
mkdir -p /path/to/paper/.claude/skills && cp -R skills/fledge /path/to/paper/.claude/skills/
```

Other agents that support the Agent Skills format can load
{download}`skills/fledge/SKILL.md <../skills/fledge/SKILL.md>` the same way. The
`fledge` executable must be on the agent's `PATH`; see
[installation](installation.md) for the launcher location.

## What the agent runs

The skill asks the agent to prefer one-line-per-finding output:

```sh
fledge check /path/to/paper --main main.tex --output-format compact --quiet
fledge rule TEX005 --json
```

Use `--json` when the agent needs complete evidence, diffs or tool versions. To
apply venue-oriented checks, the skill uses `--preset NAME` and narrows runs with
`--select` and `--ignore`. The
skill tells the agent not to edit scientific content to silence heuristics, not to
enable online checks without asking, and to report a submission as ready only after
`prepare` released `submission.zip`. A test keeps every command and option named in
the skill in sync with the CLI.
