#!/usr/bin/env node

/**
 * Unified Verification Gate for Agentic Memory
 *
 * Enforces zero-warning linting, strict type checking, doc freshness,
 * Rust invariants, UI governance, and regression testing across the monorepo.
 *
 * Usage:
 *   node scripts/verify.mjs          # Full verification gate
 *   node scripts/verify.mjs --fast   # Fast developer verification
 */

import { spawn } from "node:child_process";
import { performance } from "node:perf_hooks";

const args = process.argv.slice(2);
const isFast = args.includes("--fast");

const colors = {
  reset: "\x1b[0m",
  bold: "\x1b[1m",
  green: "\x1b[32m",
  red: "\x1b[31m",
  yellow: "\x1b[33m",
  cyan: "\x1b[36m",
  dim: "\x1b[2m",
};

/**
 * @param {string} name
 * @param {string} cmd
 * @param {string[]} cmdArgs
 * @param {string} [cwd]
 * @returns {Promise<{ name: string, durationMs: number, success: boolean, error?: string }>}
 */
function runStep(name, cmd, cmdArgs, cwd = process.cwd()) {
  return new Promise((resolve) => {
    const start = performance.now();
    process.stdout.write(`  ${colors.cyan}▶${colors.reset} ${name}... `);

    const proc = spawn(cmd, cmdArgs, {
      cwd,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, OBJC_DISABLE_INITIALIZE_FORK_SAFETY: "YES" },
    });

    let stderr = "";
    let stdout = "";

    proc.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    proc.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    proc.on("close", (code) => {
      const durationMs = Math.round(performance.now() - start);
      if (code === 0) {
        console.log(`${colors.green}✔ PASS${colors.reset} ${colors.dim}(${durationMs}ms)${colors.reset}`);
        resolve({ name, durationMs, success: true });
      } else {
        console.log(`${colors.red}✖ FAIL${colors.reset} ${colors.dim}(${durationMs}ms)${colors.reset}`);
        const output = (stderr + "\n" + stdout).trim();
        console.error(`\n${colors.dim}--- Output for ${name} ---${colors.reset}`);
        console.error(output);
        console.error(`${colors.dim}---------------------------------${colors.reset}\n`);
        resolve({ name, durationMs, success: false, error: output });
      }
    });

    proc.on("error", (err) => {
      const durationMs = Math.round(performance.now() - start);
      console.log(`${colors.red}✖ ERROR${colors.reset} ${colors.dim}(${durationMs}ms)${colors.reset}`);
      console.error(`\n${colors.red}${err.message}${colors.reset}\n`);
      resolve({ name, durationMs, success: false, error: err.message });
    });
  });
}

async function main() {
  console.log(`\n${colors.bold}====================================================${colors.reset}`);
  console.log(`${colors.bold}   Unified Verification Gate — Agentic Memory   ${colors.reset}`);
  console.log(`   Mode: ${isFast ? colors.yellow + "FAST (--fast)" : colors.green + "STRICT (FULL)"}${colors.reset}`);
  console.log(`${colors.bold}====================================================${colors.reset}\n`);

  const globalStart = performance.now();
  const results = [];

  const phases = [
    {
      title: "Phase 1: Code Quality & Zero-Warning Linting",
      steps: [
        { name: "Python Ruff Lint", cmd: "./venv/bin/ruff", args: ["check", ".", "--config", "pyproject.toml"] },
        { name: "TypeScript ESLint (IDE)", cmd: "pnpm", args: ["--dir", "ide", "lint"] },
      ],
    },
    {
      title: "Phase 2: Strict Type Checking",
      steps: [
        { name: "Python Mypy (Kernel & Surface)", cmd: "./venv/bin/mypy", args: ["--config-file", "pyproject.toml"] },
        { name: "TypeScript Typecheck (IDE)", cmd: "pnpm", args: ["--dir", "ide", "typecheck"] },
        { name: "TypeScript SDK Build & Dist Invariance", cmd: "./venv/bin/python", args: ["scripts/check_ts_sdk_drift.py"] },
      ],
    },
    {
      title: "Phase 3: Rust Crates Verification",
      steps: [
        { name: "Rust Formatting Check", cmd: "cargo", args: ["fmt", "--all", "--check"], cwd: "ide" },
        { name: "Rust Clippy (-D warnings)", cmd: "cargo", args: ["clippy", "--workspace", "--all-targets", "--", "-D", "warnings"], cwd: "ide" },
      ],
    },
    {
      title: "Phase 4: UI Governance & Invariants",
      steps: [
        { name: "UI Governance & Contrast Audit", cmd: "pnpm", args: ["--dir", "ide", "audit:ui-governance"] },
        { name: "Bundle Budget Check", cmd: "pnpm", args: ["--dir", "ide", "check:bundle"] },
        { name: "Doc & Architecture Freshness (Rule 24)", cmd: "make", args: ["update-docs"] },
      ],
    },
    {
      title: "Phase 5: Automated Testing & Regressions",
      steps: isFast
        ? [
            { name: "Python Fast Unit Tests", cmd: "./venv/bin/pytest", args: ["eval/", "-q", "-m", "not slow", "--tb=line", "-p", "no:cacheprovider", "--timeout=60"] },
            { name: "IDE Workspace Tests", cmd: "pnpm", args: ["--dir", "ide", "test"] },
          ]
        : [
            { name: "Python Fast Unit Tests", cmd: "./venv/bin/pytest", args: ["eval/", "-q", "-m", "not slow", "--tb=line", "-p", "no:cacheprovider", "--timeout=60"] },
            { name: "IDE Coverage Tests", cmd: "pnpm", args: ["--dir", "ide", "test:coverage"] },
          ],
    },
  ];

  let anyFailed = false;

  for (const phase of phases) {
    console.log(`${colors.bold}${phase.title}${colors.reset}`);
    for (const step of phase.steps) {
      const res = await runStep(step.name, step.cmd, step.args, step.cwd);
      results.push(res);
      if (!res.success) {
        anyFailed = true;
        break;
      }
    }
    console.log();
    if (anyFailed) break;
  }

  const totalDuration = ((performance.now() - globalStart) / 1000).toFixed(2);
  console.log(`${colors.bold}----------------------------------------------------${colors.reset}`);
  if (anyFailed) {
    console.log(`${colors.red}${colors.bold}✖ VERIFICATION GATE FAILED${colors.reset} in ${totalDuration}s`);
    const failedSteps = results.filter((r) => !r.success).map((r) => r.name);
    console.log(`${colors.red}Failed: ${failedSteps.join(", ")}${colors.reset}\n`);
    process.exit(1);
  } else {
    console.log(`${colors.green}${colors.bold}✔ ALL GATES PASSED CLEANLY${colors.reset} in ${totalDuration}s`);
    console.log(`${colors.green}Zero warnings, strict types, fresh docs, and passing tests verified.${colors.reset}\n`);
    process.exit(0);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
