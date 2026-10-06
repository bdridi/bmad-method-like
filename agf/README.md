# Prepare a Repo for the Agentic Factory Harness

Use `init` to prepare an existing Git repo. It installs the harness skills, sets up the `_bmad` runtime, and copies the team config into `_bmad/custom/`. It needs no registry and no agent.

`agf` stands for Agentic Factory, the team that maintains this harness.

## Prerequisites

- [uv](https://docs.astral.sh/uv/), Git, and Node.js (for `npx`) on the machine.
- A Git repo to prepare. `init` never creates a repo or a remote.
- Access to GitHub, and to the npm registry, which serves the Skills CLI that `init` calls.

## Run It

Pin a tag for everyday use:

```bash
npx github:<owner>/<repo>#<tag> init
```

Pin a commit SHA for audited deployments. A tag can be moved; a SHA cannot.

Without `#<tag-or-sha>`, `init` runs on the default branch and warns you. Add `--agent <tool>` to install for a coding tool other than `claude-code`.

`init` reads its own version from the checkout `npx` fetched and installs the skills from that same checkout.

## What It Does

Every run does the same steps, so it is safe to run again. To upgrade, run it with the new tag.

1. Installs the harness skills as copies, not symlinks. The list is `SKILLS` in `init.py`.
2. Runs `setup.py` to create or refresh `_bmad/`. A config question takes its default; one with no default stops `init` with a message naming it.
3. Creates the `_agf/logs/` folder. Adds `agf/config/custom.toml` to `_agf/` when `_agf/` does not have it yet. A file the team already has is never replaced.
4. Records the version in `_agf/custom.toml`:

   ```toml
   [harness]
   ref = "<tag>"
   sha = "<sha>"
   ```

5. Copies `_agf/custom.toml` to `_bmad/custom/config.toml`, replacing it. Other files in `_bmad/custom/`, such as `*.user.toml`, stay as they are.
6. Adds `_bmad/render/`, `*.user.toml`, and `skills-lock.json` to `.gitignore`.

## Edit the Team Config

Edit `_agf/custom.toml`, never `_bmad/custom/config.toml`: the next `init` replaces the copy. Commit `_agf/` so the whole team shares it.

A change to `agf/config/custom.toml` reaches only repos that do not have `_agf/custom.toml` yet. To adopt it elsewhere, change `_agf/custom.toml` by hand. See [Adopt BMad Across a Team](../docs/customize/adopt-bmad-across-a-team.md) for what the files can hold.

## Known Limits

- Git and GitHub must be reachable from the machine, and so must the npm registry for the Skills CLI. Nothing works offline.
- `init` does not stop an older version from replacing a newer one. Use the tag in the team's documentation.
- `init` does not run module migrations. They need your approval of a plan, so run them through the `bmad` skill.
- `init` does not delete retired skills. It lists them for you to remove.
