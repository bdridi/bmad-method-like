#!/usr/bin/env node
// Runs the Python core with uv. The core works on the current directory, which is the user's repo.
"use strict";

const { spawnSync } = require("node:child_process");
const path = require("node:path");

const probe = spawnSync("uv", ["--version"], { stdio: "ignore" });
if (probe.error || probe.status !== 0) {
  console.error("agf: uv is required and was not found. Install it: https://docs.astral.sh/uv/getting-started/installation/");
  process.exit(1);
}

const core = path.join(__dirname, "init.py");
const run = spawnSync("uv", ["run", "--script", core, ...process.argv.slice(2)], { stdio: "inherit" });
if (run.error) {
  console.error(`agf: cannot run uv: ${run.error.message}`);
  process.exit(1);
}
process.exit(run.status ?? 1);
