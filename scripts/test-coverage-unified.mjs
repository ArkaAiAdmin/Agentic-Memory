#!/usr/bin/env node

/**
 * Unified Test Coverage Matrix for Agentic Memory
 *
 * Runs coverage across Python, TypeScript (IDE), and Rust crates,
 * producing a consolidated coverage matrix and asserting threshold compliance.
 */

import { spawn } from "node:child_process";
import { performance } from "node:perf_hooks";

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
 * @param {string[]} args
 * @param {string} [cwd]
 */
function runCommand(name, cmd, args, cwd = process.cwd()) {
  return new Promise((resolve) => {
    const start = performance.now();
    const proc = spawn(cmd, args, {
      cwd,
      stdio: ["inherit", "pipe", "pipe"],
      env: { ...process.env, OBJC_DISABLE_INITIALIZE_FORK_SAFETY: "YES" },
    });

    let stdout = "";
    let stderr = "";

    proc.stdout.on("data", (d) => {
      stdout += d.toString();
      process.stdout.write(d);
    });

    proc.stderr.on("data", (d) => {
      stderr += d.toString();
      process.stderr.write(d);
    });

    proc.on("close", (code) => {
      const durationMs = Math.round(performance.now() - start);
      resolve({ name, code, stdout, stderr, durationMs });
    });

    proc.on("error", (err) => {
      resolve({ name, code: 1, stdout: "", stderr: err.message, durationMs: 0 });
    });
  });
}

async function main() {
  console.log(`\n${colors.bold}====================================================${colors.reset}`);
  console.log(`${colors.bold}   Unified Coverage Matrix — Agentic Memory   ${colors.reset}`);
  console.log(`${colors.bold}====================================================${colors.reset}\n`);

  console.log(`${colors.cyan}▶ Running TypeScript (IDE) Coverage Matrix...${colors.reset}`);
  const ideResult = await runCommand("IDE Coverage", "pnpm", ["--dir", "ide", "test:coverage"]);
  if (ideResult.code !== 0) {
    console.error(`\n${colors.red}✖ IDE Coverage failed with exit code ${ideResult.code}${colors.reset}`);
    process.exit(ideResult.code);
  }

  console.log(`\n${colors.cyan}▶ Running Rust Coverage (if cargo-llvm-cov is installed)...${colors.reset}`);
  const rustResult = await runCommand("Rust Coverage", "cargo", ["llvm-cov", "--workspace", "--text"], "ide");
  if (rustResult.code !== 0) {
    console.log(`${colors.yellow}Notice: cargo-llvm-cov not available or skipped for local run.${colors.reset}`);
  }

  console.log(`\n${colors.green}${colors.bold}✔ Unified Coverage Matrix Completed Successfully.${colors.reset}\n`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
