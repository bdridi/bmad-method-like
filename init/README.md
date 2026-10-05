# Prepare a Repo for a Profile

Use `init` to prepare an existing Git repo for a role. It installs the role's skills, creates the `_bmad` runtime, and commits to one version of this repo for the whole team. It needs no registry and no agent.

## Prerequisites

- [uv](https://docs.astral.sh/uv/), Git, and Node.js (for `npx`) on the machine.
- A Git repo to prepare. `init` never creates a repo or a remote.
- Access to GitHub, and to the npm registry, which serves the Skills CLI that `init` calls.

## Run It

Pin a tag for everyday use:

```bash
npx github:<owner>/<repo>#<tag> init --profile ba
```

Pin a commit SHA for audited deployments. A tag can be moved; a SHA cannot.

```bash
npx github:<owner>/<repo>#<sha> init --profile ba
```

Without `#<tag-or-sha>`, `init` runs on the default branch and warns you. Add `--agent <tool>` to install for a coding tool other than the profile's default.

`init` reads its own version from the checkout `npx` fetched, installs the skills from that same checkout, and records the version in `_bmad/custom/config.toml`:

```toml
[harness]
ref = "<tag>"
sha = "<sha>"
```

## What It Does

`init` looks at the repo and takes one of three paths. It is safe to run again.

| Repo state | What `init` does |
| --- | --- |
| No `_bmad/` | Installs the profile's skills with copies, not symlinks. Creates `_bmad/` with `setup.py`, using the profile's answers. Copies the profile's team config to `_bmad/custom/`, records the version, and adds `_bmad/render/`, `*.user.toml`, and `skills-lock.json` to `.gitignore`. |
| `_bmad/` exists | Changes no shared file and no existing value. Installs only the missing skills, refreshes the runtime through `setup.py`, and reports what it did. |
| This run is newer than the recorded version | Reinstalls the profile's skills, runs `setup.py` so renamed skills keep their customizations, and moves the recorded version. |

A run older than the recorded version stops and prints the command that matches the team's version.

`init` is not interactive. A config question with neither a profile answer nor a default stops it with a message naming the question.

## Write a Profile

A profile is a folder under `profiles/` and needs no code:

| File | Content |
| --- | --- |
| `profile.toml` | `skills`, the list to install (it must include `bmad`), and `agent`, the default coding tool. |
| `answers.toml` | Answers to module config questions, in the `[modules."<code>"]` format that `setup.py` reads. Optional. |
| `custom/*.toml` | Team overrides copied to `_bmad/custom/` when the runtime is first created. See [Adopt BMad Across a Team](../docs/customize/adopt-bmad-across-a-team.md). |

## Known Limits

- Git and GitHub must be reachable from the machine, and so must the npm registry for the Skills CLI. Nothing works offline.
- `init` does not run module migrations. They need your approval of a plan, so run them through the `bmad` skill.
- `init` does not delete retired skills. It lists them for you to remove.
