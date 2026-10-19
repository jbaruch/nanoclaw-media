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

- Each target's top-of-file docstring documents its arguments, stdout shape and exit codes
- Script and calling-step requirements beyond that: see `coding-policy: script-delegation` and `coding-policy: script-as-black-box`

## Regression Coverage

- `tests/test_skill_script_paths.py` resolves every such path in every `SKILL.md` to a declared skill and a co-shipped script
- The same test requires each target's docstring to name its stdout shape and exit codes
