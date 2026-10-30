"use strict";

const { spawn } = require("node:child_process");

const { BootstrapError } = require("./errors");

const UV_INSTALL_URL = "https://astral.sh/uv/install.sh";

function installUv(spawnImpl = spawn) {
  return new Promise((resolve) => {
    let curl;
    try {
      curl = spawnImpl("curl", ["-LsSf", UV_INSTALL_URL], {
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (error) {
      resolve({ code: 1, stdout: "", stderr: error.message });
      return;
    }
    const shell = spawnImpl("sh", [], {
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stderr = "";
    curl.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    shell.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    curl.stdout.pipe(shell.stdin);
    curl.on("error", (error) => {
      stderr += error.message;
      shell.stdin.end();
    });
    shell.on("error", (error) => {
      resolve({ code: 1, stdout: "", stderr: `${stderr}${error.message}` });
    });
    shell.on("close", (code) => {
      resolve({ code: code === null ? 1 : code, stdout: "", stderr });
    });
  });
}

async function ensureUv({ find, install, log }) {
  const existing = find("uv");
  if (existing) {
    return { command: existing, installed: false };
  }
  log("uv was not found.");
  log(`Installing uv from ${UV_INSTALL_URL}`);
  log("This downloads uv into your home directory. It does not use sudo or change system Python.");
  const result = await install();
  if (result.code !== 0) {
    throw new BootstrapError(
      "uv could not be installed. Install it from https://docs.astral.sh/uv/getting-started/installation/ and run this command again.",
    );
  }
  const installed = find("uv");
  if (!installed) {
    throw new BootstrapError(
      "uv was installed but is not available yet. Add \"$HOME/.local/bin\" to PATH, then run this command again.",
    );
  }
  return { command: installed, installed: true };
}

module.exports = { UV_INSTALL_URL, ensureUv, installUv };
