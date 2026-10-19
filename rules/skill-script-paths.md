---
alwaysApply: false
applyTo: "skills/**/SKILL.md — when a skill step runs one of this plugin's scripts by path"
---

# Skill Script Paths

## Scope

- Governs every `skills/<name>/SKILL.md` in `jbaruch/nanoclaw-media`
- Authority-of-record for this plugin's scoped skill lookup under `coding-policy: script-delegation` Scoped Plugin Contracts
- Grants no runtime authority and waives no check after lookup

## Co-Shipped Targets

- A step runs a script at `/home/node/.claude/skills/tessl__<name>/scripts/<script>`
- NanoClaw's container runner installs each skill this plugin declares at `/home/node/.claude/skills/tessl__<name>/`
- `<name>` is a skill listed in `.tessl-plugin/plugin.json` `skills`
- `<script>` is a file at `skills/<name>/scripts/<script>` in this repository
- A path naming any other skill or file is out of scope

## Input/Output Contract

- Each target's top-of-file docstring owns its arguments, stdout shape and exit codes
- The calling step quotes that contract; it never restates the script's internal logic
- A target that writes JSON emits it on stdout; diagnostics go to stderr

## Regression Coverage

- `tests/test_skill_script_paths.py` resolves every such path in every `SKILL.md` to a declared skill and a co-shipped script
