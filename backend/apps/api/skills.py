"""Agent-skills wiki: discovery + on-demand reading of SKILL.md files.

Feature-flagged by settings.api.skills_enabled (default off — purely
additive, existing tool-calling behavior is unchanged until it's turned on).
Follows the Agent Skills open format (https://agentskills.io/specification),
the same one Claude Code itself uses: a folder per skill under
settings.api.skills_dir, each containing SKILL.md with YAML frontmatter
(name, description, ...) plus a markdown body.

Progressive disclosure, same principle as Claude Code's own Skill tool:
list_skills (streaming_agent.py) exposes only name+description — cheap
enough to be always affordable in a tool result — and get_skill reads one
skill's full body only once the model has decided it's relevant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger("agentic_api")


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    path: Path


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a SKILL.md file into (frontmatter dict, body).

    Returns ({}, text) if the file has no `---`-delimited frontmatter block
    or the block doesn't parse as a YAML mapping — callers treat that as
    "not a valid skill" rather than crashing the whole index over one bad
    file.
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    _, raw_frontmatter, body = parts
    try:
        frontmatter = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError as exc:
        logger.warning(f"[SKILLS] Malformed frontmatter: {exc}")
        return {}, text
    if not isinstance(frontmatter, dict):
        return {}, text
    return frontmatter, body.lstrip("\n")


def load_skill_index(skills_dir: Path) -> list[SkillMeta]:
    """Scan `skills_dir` for `<name>/SKILL.md` folders and return their
    (name, description) metadata.

    Skips anything malformed (missing frontmatter, name/folder mismatch)
    with a warning rather than failing the whole index — one bad skill
    shouldn't take every other one down with it.
    """
    if not skills_dir.is_dir():
        return []

    skills: list[SkillMeta] = []
    for entry in sorted(skills_dir.iterdir()):
        skill_file = entry / "SKILL.md"
        if not entry.is_dir() or not skill_file.is_file():
            continue
        try:
            frontmatter, _ = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning(f"[SKILLS] Could not read {skill_file}: {exc}")
            continue

        name = frontmatter.get("name")
        description = frontmatter.get("description")
        if not name or not description:
            logger.warning(f"[SKILLS] {skill_file} missing required 'name'/'description' frontmatter, skipping")
            continue
        if name != entry.name:
            logger.warning(f"[SKILLS] {skill_file} name={name!r} does not match folder {entry.name!r}, skipping")
            continue

        skills.append(SkillMeta(name=name, description=description, path=skill_file))

    return skills


def read_skill_body(skills_dir: Path, name: str) -> str | None:
    """Read one skill's full markdown body (frontmatter stripped) by name.

    Returns None if `name` doesn't resolve to a real skill under
    `skills_dir` — callers turn that into an error tool result, not an
    exception, since a model guessing a wrong skill name is an expected
    failure mode, not a bug.
    """
    # Path(name).name strips any directory components, so a crafted
    # "../../something" argument can't escape skills_dir.
    if not name or name != Path(name).name:
        return None
    skill_file = skills_dir / name / "SKILL.md"
    if not skill_file.is_file():
        return None
    _, body = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    return body.strip()
