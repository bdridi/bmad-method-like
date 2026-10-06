#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Prepare a working repo for the Agentic Factory harness: install its skills, set up `_bmad`, sync the team config.

Runs from the checkout that npx fetched, so that checkout is both the installer and the source of the skills:
the version it is on is the version that gets installed and recorded in the working repo.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_CLI = "skills@1.4.6"
DEFAULT_AGENT = "claude-code"
# The skills every working repo gets: the harness is one product, not one set per role.
SKILLS = (
    "bmad",
    "bmod-core-tools",
    "bmod-method",
    "bmad-agent-analyst",
    "bmad-agent-pm",
    "bmad-brainstorming",
    "bmad-product-brief",
    "bmad-prd",
    "bmad-spec",
    "bmad-project-context",
    "bmad-review",
    "bmad-ticket",
)
CONFIG_SOURCE = REPO_ROOT / "agf" / "config"
# _agf holds the team config of the working repo. init copies it into _bmad/custom, so _bmad never carries
# configuration of its own. _agf/custom.toml becomes _bmad/custom/config.toml, the other files keep their name.
CONFIG_DIR = Path("_agf")
CONFIG_FILE = CONFIG_DIR / "custom.toml"
LOGS_DIR = CONFIG_DIR / "logs"
CUSTOM_DIR = Path("_bmad/custom")
HARNESS_TABLE = "harness"
# skills-lock.json records the checkout's path on this machine, which must not be committed.
GITIGNORE_LINES = ("_bmad/render/", "*.user.toml", "skills-lock.json")
GITHUB_SLUG = re.compile(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?/?(?:#|$)")
RESOLVED_SHA = re.compile(r"#(?P<sha>[0-9a-f]{40})$")


class InitError(Exception):
    """A failure the user can act on; main prints it without a traceback."""


class Version(NamedTuple):
    source: str | None  # "owner/repo" on GitHub, None when the origin is not a GitHub URL
    ref: str | None  # the tag, branch or commit the user asked for; None on the default branch
    sha: str


Installer = Callable[[Path, Path, str, list[str]], None]


# --- Version ---------------------------------------------------------------------------------------------------


def git(root: Path, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return result.stdout.strip() or None if result.returncode == 0 else None


def github_slug(url: str | None) -> str | None:
    match = GITHUB_SLUG.search(url or "")
    return match.group("slug") if match else None


def determine_version(root: Path = REPO_ROOT) -> Version:
    """The version of the checkout this script runs from: a git clone, or the package npx unpacked."""
    if (root / ".git").exists():
        sha = git(root, "rev-parse", "HEAD")
        if sha is None:
            raise InitError(f"cannot read the commit of {root}")
        return Version(
            github_slug(git(root, "remote", "get-url", "origin")), git(root, "describe", "--tags", "--exact-match"), sha
        )
    # npx unpacks into <cache>/node_modules/<name>; npm records the resolved commit beside it
    # and the requested spec (with its #ref) in the cache's own package.json.
    name = root.name
    try:
        lock = json.loads((root.parent / ".package-lock.json").read_text(encoding="utf-8"))
        resolved = lock["packages"][f"node_modules/{name}"]["resolved"]
        spec = json.loads((root.parent.parent / "package.json").read_text(encoding="utf-8"))["dependencies"][name]
    except (OSError, ValueError, KeyError) as error:
        raise InitError(
            f"cannot determine the version of {root}: not a git clone and no npx metadata ({error!r})"
        ) from error
    sha = RESOLVED_SHA.search(resolved)
    if sha is None:
        raise InitError(f"cannot determine the commit npx resolved: {resolved}")
    ref = spec.partition("#")[2] or None
    return Version(github_slug(resolved), ref, sha.group("sha"))


def version_label(version: Version) -> str:
    return f"{version.ref} ({version.sha[:12]})" if version.ref else version.sha[:12]


def write_version(project: Path, version: Version) -> bool:
    """Set the [harness] table of _agf/custom.toml, leaving every other line as it is. True when the file changed."""
    path = project / CONFIG_FILE
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    fields = {"source": version.source, "ref": version.ref, "sha": version.sha}
    block = f"[{HARNESS_TABLE}]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in fields.items() if value)
    section = re.compile(rf"^\[{HARNESS_TABLE}\]\n(?:[^\[\n].*\n?)*", re.MULTILINE)
    if section.search(text):
        updated = section.sub(lambda _: block, text, count=1)
    else:
        updated = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    if updated == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8", newline="")
    return True


# --- Answers ---------------------------------------------------------------------------------------------------


def resolve_answers(questions: list[dict]) -> dict[str, dict[str, str]]:
    """One answer per pending question: the question's default. A question with none is an error."""
    answers: dict[str, dict[str, str]] = {}
    unanswered = []
    for question in questions:
        module, key = question["module"], question["key"]
        if question["default"] == "":
            unanswered.append(f"{module}.{key} ({question['prompt']})")
        else:
            answers.setdefault(module, {})[key] = question["default"]
    if unanswered:
        raise InitError("the module has no default for: " + "; ".join(unanswered))
    return answers


def answers_toml(answers: dict[str, dict[str, str]]) -> str:
    lines = []
    for module, values in answers.items():
        lines.append(f"[modules.{json.dumps(module)}]")
        lines += [f"{json.dumps(key)} = {json.dumps(value)}" for key, value in values.items()]
        lines.append("")
    return "\n".join(lines)


# --- Working repo ----------------------------------------------------------------------------------------------


def check_prerequisites() -> dict[str, str]:
    hints = {
        "uv": "install uv (https://docs.astral.sh/uv/getting-started/installation/)",
        "git": "install Git (https://git-scm.com/downloads)",
        "npx": "install Node.js, which includes npx (https://nodejs.org/)",
    }
    found = {tool: shutil.which(tool) for tool in hints}
    missing = [f"{tool}: {hints[tool]}" for tool, path in found.items() if path is None]
    if missing:
        raise InitError("missing prerequisites:\n  " + "\n  ".join(missing))
    return found  # type: ignore[return-value]


def project_root() -> Path:
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if result.returncode != 0:
        raise InitError(
            "not inside a Git repository. init fills an existing repo and never creates one: run `git init` first."
        )
    return Path(result.stdout.strip())


def skill_roots(project: Path) -> list[Path]:
    return sorted(path for path in project.glob(".*/skills") if path.is_dir())


def present_skills(project: Path) -> set[str]:
    return {skill.name for root in skill_roots(project) for skill in root.iterdir() if (skill / "SKILL.md").is_file()}


def npx_install(project: Path, source: Path, agent: str, skills: list[str]) -> None:
    """Install from the local checkout: it is the pinned version, whatever the ref was (a commit cannot be cloned by name)."""
    npx = shutil.which("npx") or "npx"
    command = [npx, "--yes", SKILLS_CLI, "add", str(source), "--skill", *skills, "--agent", agent, "-y", "--copy"]
    result = subprocess.run(command, cwd=project, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-15:])
        raise InitError(f"skills installation failed ({' '.join(command[1:6])} ...):\n{tail}")


def call_setup(project: Path, *flags: str | Path) -> object:
    """Run bmad's setup.py as the agent does and return its JSON."""
    roots = skill_roots(project)
    skill = next((root / "bmad" for root in roots if (root / "bmad" / "SKILL.md").is_file()), None)
    if skill is None:
        raise InitError("the bmad skill is not installed in the project")
    command = [
        "uv",
        "run",
        "--no-cache",
        str(skill / "scripts" / "setup.py"),
        "--project-root",
        str(project),
        "--skill",
        str(skill),
    ]
    for root in roots:
        command += ["--root", str(root)]
    result = subprocess.run([*command, *map(str, flags)], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise InitError(f"setup.py failed: {(result.stderr or result.stdout).strip()}")
    return json.loads(result.stdout)


def ensure_gitignore(project: Path, lines: tuple[str, ...] = GITIGNORE_LINES) -> list[str]:
    path = project / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""

    def key(line: str) -> str:
        return line.strip().strip("/")

    present = {key(line) for line in text.splitlines()}
    missing = [line for line in lines if key(line) not in present]
    if missing:
        lead = "\n" if text and not text.endswith("\n") else ""
        with path.open("a", encoding="utf-8", newline="") as file:
            file.write(lead + "".join(f"{line}\n" for line in missing))
    return missing


def seed_config(project: Path, source: Path) -> list[str]:
    """Copy the harness defaults into _agf. A file the team already has is theirs and is left alone."""
    added = []
    for file in sorted(source.glob("*.toml")):
        target = project / CONFIG_DIR / file.name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
            added.append(file.name)
    return added


def sync_custom(project: Path) -> list[str]:
    """Copy _agf into _bmad/custom, replacing what is there. Files _agf does not have, such as *.user.toml, stay."""
    changed = []
    for file in sorted((project / CONFIG_DIR).glob("*.toml")):
        target = project / CUSTOM_DIR / ("config.toml" if file.name == "custom.toml" else file.name)
        content = file.read_bytes()
        if not target.is_file() or target.read_bytes() != content:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            changed.append(target.name)
    return changed


class Report(NamedTuple):
    version: Version
    installed: list[str]
    setup_status: str
    answers_added: list[str]
    config_seeded: list[str]
    version_written: bool
    custom_synced: list[str]
    gitignore_added: list[str]
    warnings: list[str]
    check: dict


def run_init(
    project: Path,
    agent: str,
    version: Version,
    installer: Installer = npx_install,
    skills: Sequence[str] = SKILLS,
    config: Path = CONFIG_SOURCE,
) -> Report:
    warnings = []
    if version.ref is None:
        warnings.append(
            f"no tag or commit was given, so init ran on the default branch at {version.sha[:12]}. "
            "Pin it: npx github:<owner>/<repo>#<tag-or-sha> init"
        )
    installer(project, REPO_ROOT, agent, list(skills))
    present = present_skills(project)
    absent = [skill for skill in skills if skill not in present]
    if absent:
        raise InitError(f"skills missing after installation: {', '.join(absent)}")

    questions = call_setup(project, "--list-config-questions")
    if not isinstance(questions, list):
        raise InitError(f"unexpected answer to --list-config-questions: {questions}")
    answers = resolve_answers(questions)
    with tempfile.TemporaryDirectory() as scratch:
        flags: list[str | Path] = []
        if answers:
            answers_file = Path(scratch) / "answers.toml"
            answers_file.write_text(answers_toml(answers), encoding="utf-8")
            flags = ["--module-answers", answers_file]
        setup = call_setup(project, *flags)

    (project / LOGS_DIR).mkdir(parents=True, exist_ok=True)
    config_seeded = seed_config(project, config)
    version_written = write_version(project, version)
    custom_synced = sync_custom(project)
    gitignore_added = ensure_gitignore(project)
    check = call_setup(project, "--status")
    return Report(
        version,
        list(skills),
        setup["status"],
        [f"{a['module']}.{a['key']}" for a in setup["answers_added"]],
        config_seeded,
        version_written,
        custom_synced,
        gitignore_added,
        warnings,
        check,
    )


def format_report(report: Report) -> str:
    check = report.check
    lines = [f"init: {version_label(report.version)}"]
    lines += [f"warning: {warning}" for warning in report.warnings]
    lines.append("skills: " + ", ".join(report.installed))
    lines.append(f"runtime (_bmad): {report.setup_status}")
    if report.answers_added:
        lines.append("config answers added: " + ", ".join(report.answers_added))
    if report.config_seeded:
        lines.append(f"team config added to {CONFIG_DIR.as_posix()}: " + ", ".join(report.config_seeded))
    if report.version_written:
        lines.append(f"version recorded in {CONFIG_FILE.as_posix()}")
    if report.custom_synced:
        lines.append(f"copied to {CUSTOM_DIR.as_posix()}: " + ", ".join(report.custom_synced))
    if report.gitignore_added:
        lines.append(".gitignore: added " + ", ".join(report.gitignore_added))
    for key in ("problems", "retired_skills", "custom_not_renamed", "custom_unused", "unmet_requirements"):
        if check.get(key):
            lines.append(f"{key}: {json.dumps(check[key], ensure_ascii=False)}")
    if check.get("next"):
        lines.append(f"next: {check['next']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=bin_name(), description="Prepare a working repo for the harness.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="install the harness skills, set up the BMad runtime and sync the team config"
    )
    init.add_argument("--agent", default=DEFAULT_AGENT, help="the coding tool to install skills for")
    args = parser.parse_args(argv)
    try:
        check_prerequisites()
        print(format_report(run_init(project_root(), args.agent, determine_version())))
    except InitError as error:
        print(f"{bin_name()}: error: {error}", file=sys.stderr)
        return 1
    return 0


def bin_name() -> str:
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    return next(iter(package["bin"]))


if __name__ == "__main__":
    sys.exit(main())
