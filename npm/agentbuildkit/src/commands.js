"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function findExecutable(name, pathValue = process.env.PATH || "", home = os.homedir()) {
  const dirs = String(pathValue)
    .split(path.delimiter)
    .filter((dir) => dir !== "");
  dirs.push(path.join(home, ".local", "bin"));
  for (const dir of dirs) {
    const candidate = path.join(dir, name);
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch {
      // Keep searching PATH and the uv default install directory.
    }
  }
  return null;
}

function runCommand(command, args, options = {}) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(command, args, {
        cwd: options.cwd,
        env: options.env || process.env,
        shell: false,
        stdio: options.stdio === "inherit" ? "inherit" : ["ignore", "pipe", "pipe"],
      });
    } catch (error) {
      resolve({ code: 1, stdout: "", stderr: error.message });
      return;
    }
    if (options.stdio === "inherit") {
      child.on("error", (error) => {
        resolve({ code: 1, stdout: "", stderr: error.message });
      });
      child.on("close", (code) => {
        resolve({ code: code === null ? 1 : code, stdout: "", stderr: "" });
      });
      return;
    }
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      resolve({ code: 1, stdout, stderr: `${stderr}${error.message}` });
    });
    child.on("close", (code) => {
      resolve({ code: code === null ? 1 : code, stdout, stderr });
    });
  });
}

module.exports = { findExecutable, runCommand };
