#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Prepare a working repo for a profile: install its skills, create `_bmad` with setup.py, deposit the team config.

Runs from the checkout that npx fetched, so that checkout is both the installer and the source of the skills:
the version it is on is the version that gets installed and pinned in the working repo.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_CLI = "skills@1.4.6"
PIN_FILE = Path("_bmad/custom/config.toml")
PIN_TABLE = "harness"
# skills-lock.json records the checkout's path on this machine, which must not be committed.
GITIGNORE_LINES = ("_bmad/render/", "*.user.toml", "skills-lock.json")
SEMVER_REF = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
GITHUB_SLUG = re.compile(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?/?(?:#|$)")
RESOLVED_SHA = re.compile(r"#(?P<sha>[0-9a-f]{40})$")
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


class InitError(Exception):
    """A failure the user can act on; main prints it without a traceback."""


class Version(NamedTuple):
    source: str | None  # "owner/repo" on GitHub, None when the origin is not a GitHub URL
    ref: str | None  # the tag, branch or commit the user asked for; None on the default branch
    sha: str


class Profile(NamedTuple):
    name: str
    agent: str | None
    skills: tuple[str, ...]
    answers: dict[tuple[str, str], str]
    custom: dict[str, Path]


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


def compare_versions(current: Version, pinned: Version) -> str:
    """same, newer, older, or changed when the two cannot be ordered (a commit has no order)."""
    if current.sha == pinned.sha:
        return "same"
    now, then = (SEMVER_REF.match(v.ref or "") for v in (current, pinned))
    if now is None or then is None:
        return "changed"
    order = (tuple(map(int, now.groups())) > tuple(map(int, then.groups()))) - (
        tuple(map(int, now.groups())) < tuple(map(int, then.groups()))
    )
    return {1: "newer", -1: "older", 0: "changed"}[order]


def read_pin(project: Path) -> Version | None:
    path = project / PIN_FILE
    if not path.is_file():
        return None
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get(PIN_TABLE)
    except tomllib.TOMLDecodeError as error:
        raise InitError(f"{PIN_FILE.as_posix()} is not valid TOML: {error}") from error
    if not isinstance(table, dict) or not isinstance(table.get("sha"), str):
        return None
    return Version(table.get("source"), table.get("ref"), table["sha"])


def write_pin(project: Path, version: Version) -> bool:
    """Set the [harness] table of the team config, leaving every other line as it is. True when the file changed."""
    path = project / PIN_FILE
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    fields = {"source": version.source, "ref": version.ref, "sha": version.sha}
    block = f"[{PIN_TABLE}]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in fields.items() if value)
    section = re.compile(rf"^\[{PIN_TABLE}\]\n(?:[^\[\n].*\n?)*", re.MULTILINE)
    if section.search(text):
        updated = section.sub(lambda _: block, text, count=1)
    else:
        updated = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    if updated == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8", newline="")
    return True


# --- Profile ---------------------------------------------------------------------------------------------------


def load_profile(name: str, profiles: Path = REPO_ROOT / "profiles") -> Profile:
    folder = profiles / name
    if not PROFILE_NAME.match(name) or not (folder / "profile.toml").is_file():
        available = (
            sorted(p.name for p in profiles.iterdir() if (p / "profile.toml").is_file()) if profiles.is_dir() else []
        )
        raise InitError(f"unknown profile {name!r}; available: {', '.join(available) or 'none'}")
    try:
        data = tomllib.loads((folder / "profile.toml").read_text(encoding="utf-8"))
        answers_file = folder / "answers.toml"
        raw = tomllib.loads(answers_file.read_text(encoding="utf-8")) if answers_file.is_file() else {}
    except tomllib.TOMLDecodeError as error:
        raise InitError(f"profile {name!r}: {error}") from error
    skills = data.get("skills")
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills) or "bmad" not in skills:
        raise InitError(f"profile {name!r}: 'skills' must be a list of names that includes 'bmad'")
    answers = {
        (module, key): value for module, values in raw.get("modules", {}).items() for key, value in flatten(values)
    }
    custom = {p.name: p for p in sorted((folder / "custom").glob("*.toml"))} if (folder / "custom").is_dir() else {}
    return Profile(name, data.get("agent"), tuple(skills), answers, custom)


def flatten(values: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Answers keyed by dotted path, as setup.py names them: nested tables become dotted keys."""
    flat: list[tuple[str, str]] = []
    for key, value in values.items():
        if isinstance(value, dict):
            flat += flatten(value, f"{prefix}{key}.")
        else:
            flat.append((f"{prefix}{key}", value))
    return flat


def resolve_answers(questions: list[dict], profile: Profile) -> dict[str, dict[str, str]]:
    """One answer per pending question: the profile's, else the question's default. Neither is an error."""
    answers: dict[str, dict[str, str]] = {}
    unanswered = []
    for question in questions:
        module, key = question["module"], question["key"]
        value = profile.answers.get((module, key), question["default"])
        if value == "":
            unanswered.append(f"{module}.{key} ({question['prompt']})")
        else:
            answers.setdefault(module, {})[key] = value
    if unanswered:
        raise InitError(
            f"profile {profile.name!r} has no answer and the module has no default for: " + "; ".join(unanswered)
        )
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


class Report(NamedTuple):
    mode: str
    version: Version
    installed: list[str]
    setup_status: str
    answers_added: list[str]
    custom_written: list[str]
    pin_written: bool
    gitignore_added: list[str]
    warnings: list[str]
    check: dict


def run_init(
    project: Path, profile: Profile, agent: str, version: Version, installer: Installer = npx_install
) -> Report:
    pin = read_pin(project)
    warnings = []
    if version.ref is None:
        warnings.append(
            f"no tag or commit was given, so init ran on the default branch at {version.sha[:12]}. "
            "Pin it: npx github:<owner>/<repo>#<tag-or-sha> init ..."
        )
    if not (project / "_bmad").exists():
        mode = "create"
    elif pin is None:
        mode = "join"
        warnings.append(
            f"{PIN_FILE.as_posix()} has no [{PIN_TABLE}] pin, so the version is not checked against the team's."
        )
    else:
        relation = compare_versions(version, pin)
        if relation == "older":
            hint = f"npx github:{pin.source or '<owner>/<repo>'}#{pin.ref or pin.sha} init --profile {profile.name}"
            raise InitError(
                f"the team config pins {version_label(pin)}, newer than this run ({version_label(version)}). "
                f"Rerun with the pinned version:\n  {hint}"
            )
        mode = "join" if relation == "same" else "update"

    wanted = list(profile.skills)
    to_install = wanted if mode != "join" else [skill for skill in wanted if skill not in present_skills(project)]
    if to_install:
        installer(project, REPO_ROOT, agent, to_install)
    absent = [skill for skill in wanted if skill not in present_skills(project)]
    if absent:
        raise InitError(f"skills missing after installation: {', '.join(absent)}")

    questions = call_setup(project, "--list-config-questions")
    if not isinstance(questions, list):
        raise InitError(f"unexpected answer to --list-config-questions: {questions}")
    answers = resolve_answers(questions, profile)
    with tempfile.TemporaryDirectory() as scratch:
        flags: list[str | Path] = []
        if answers:
            answers_file = Path(scratch) / "answers.toml"
            answers_file.write_text(answers_toml(answers), encoding="utf-8")
            flags = ["--module-answers", answers_file]
        setup = call_setup(project, *flags)

    custom_written = []
    if mode == "create":
        custom = project / "_bmad" / "custom"
        for name, source in profile.custom.items():
            if not (custom / name).exists():
                shutil.copyfile(source, custom / name)
                custom_written.append(name)
    pin_written = write_pin(project, version) if mode != "join" else False
    gitignore_added = ensure_gitignore(project)
    check = call_setup(project, "--status")
    return Report(
        mode,
        version,
        to_install,
        setup["status"],
        [f"{a['module']}.{a['key']}" for a in setup["answers_added"]],
        custom_written,
        pin_written,
        gitignore_added,
        warnings,
        check,
    )


def format_report(report: Report) -> str:
    check = report.check
    lines = [f"init: {report.mode} ({version_label(report.version)})"]
    lines += [f"warning: {warning}" for warning in report.warnings]
    lines.append(f"skills: {'installed ' + ', '.join(report.installed) if report.installed else 'already present'}")
    lines.append(f"runtime (_bmad): {report.setup_status}")
    if report.answers_added:
        lines.append("config answers added: " + ", ".join(report.answers_added))
    if report.custom_written:
        lines.append("team config deposited in _bmad/custom: " + ", ".join(report.custom_written))
    if report.pin_written:
        lines.append(f"version pinned in {PIN_FILE.as_posix()}")
    if report.gitignore_added:
        lines.append(".gitignore: added " + ", ".join(report.gitignore_added))
    for key in ("problems", "retired_skills", "custom_not_renamed", "custom_unused", "unmet_requirements"):
        if check.get(key):
            lines.append(f"{key}: {json.dumps(check[key], ensure_ascii=False)}")
    if check.get("next"):
        lines.append(f"next: {check['next']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=bin_name(), description="Prepare a working repo for a profile.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="install a profile's skills and set up the BMad runtime in this repo")
    init.add_argument("--profile", required=True, help="a folder name under profiles/")
    init.add_argument("--agent", help="the coding tool to install skills for (default: the profile's)")
    args = parser.parse_args(argv)
    try:
        check_prerequisites()
        project = project_root()
        profile = load_profile(args.profile)
        agent = args.agent or profile.agent
        if not agent:
            raise InitError(f"no agent: pass --agent or set 'agent' in profiles/{profile.name}/profile.toml")
        print(format_report(run_init(project, profile, agent, determine_version())))
    except InitError as error:
        print(f"{bin_name()}: error: {error}", file=sys.stderr)
        return 1
    return 0


def bin_name() -> str:
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    return next(iter(package["bin"]))


if __name__ == "__main__":
    sys.exit(main())
