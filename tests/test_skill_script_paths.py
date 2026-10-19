"""Every script path a SKILL.md runs resolves to a co-shipped script.

Enforces `rules/skill-script-paths.md`: a `/home/node/.claude/skills/tessl__<name>/
scripts/<script>` path names a skill declared in the plugin manifest and a file
at `skills/<name>/scripts/<script>` in this repository.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MOUNT_PATH = re.compile(
    r"/home/node/\.claude/skills/tessl__([A-Za-z0-9_-]+)/scripts/([^\s`'\"\\)]+)"
)


def _declared_skills() -> set[str]:
    manifest = json.loads((REPO_ROOT / ".tessl-plugin/plugin.json").read_text())
    return {Path(entry).name for entry in manifest["skills"]}


def _references() -> list[tuple[str, str, str]]:
    found = []
    for skill_md in sorted(REPO_ROOT.glob("skills/*/SKILL.md")):
        for name, script in MOUNT_PATH.findall(skill_md.read_text()):
            found.append((skill_md.parent.name, name, script))
    return found


def test_skills_reference_at_least_one_script_path():
    assert _references(), "pattern matched nothing — check MOUNT_PATH against the SKILL.md files"


@pytest.mark.parametrize(("caller", "name", "script"), _references())
def test_script_path_names_a_declared_skill_and_a_co_shipped_script(caller, name, script):
    assert name in _declared_skills(), f"{caller}: tessl__{name} is not a declared skill"
    assert (REPO_ROOT / "skills" / name / "scripts" / script).is_file(), (
        f"{caller}: skills/{name}/scripts/{script} is not in this repository"
    )


def test_the_rule_is_declared_in_the_manifest():
    manifest = json.loads((REPO_ROOT / ".tessl-plugin/plugin.json").read_text())
    assert "rules/skill-script-paths.md" in manifest.get("rules", [])
