"use strict";

const path = require("node:path");

const { BootstrapError } = require("./errors");
const { installArguments, templatesReady, toolIsInstalled } = require("./source");

async function installProduct({ uv, source, upgrade, run, log, locate }) {
  const listed = await run(uv, ["tool", "list"]);
  if (listed.code !== 0) {
    throw new BootstrapError(failureDetail("uv could not list installed tools.", listed));
  }
  const present = toolIsInstalled(listed.stdout);
  let force = Boolean(upgrade);
  if (present && !force) {
    const agentkit = await locate();
    const probe = await run(agentkit.command, ["doctor"]);
    if (probe.code !== 0 || !templatesReady(probe.stdout)) {
      log("An older harness-only installation is missing the templates package.");
      log("Installing the compatible harness and templates.");
      force = true;
    } else {
      return { already: true };
    }
  }
  if (source.kind === "local") {
    log("Installing AgentBuildKit from the local repository.");
  } else {
    log("Installing AgentBuildKit with uv.");
  }
  const result = await run(uv, installArguments(source, { upgrade: present || force }));
  if (result.code !== 0) {
    if (/already installed/i.test(`${result.stdout}\n${result.stderr}`) && !force) {
      return { already: true };
    }
    throw new BootstrapError(failureDetail("AgentBuildKit could not be installed.", result));
  }
  return { already: false, upgraded: force && present };
}

async function locateAgentkit({ uv, run, find }) {
  const located = await run(uv, ["tool", "dir", "--bin"]);
  if (located.code !== 0) {
    throw new BootstrapError(failureDetail("uv could not report its tool directory.", located));
  }
  const binDir = located.stdout.trim();
  if (binDir === "") {
    throw new BootstrapError("uv did not report a tool binary directory.");
  }
  return {
    command: path.join(binDir, "agentkit"),
    binDir,
    onPath: Boolean(find("agentkit")),
  };
}

async function runDoctor(agentkit, run) {
  const result = await run(agentkit, ["doctor"]);
  if (result.code !== 0) {
    throw new BootstrapError(
      failureDetail(
        "agentkit doctor failed. Fix the problem above, then run `agentkit doctor`.",
        result,
      ),
    );
  }
  return result;
}

function failureDetail(summary, result) {
  const detail = `${result.stderr || ""}${result.stdout || ""}`.trim();
  const network = /network|offline|failed to fetch|temporary failure|could not resolve|nodename/i.test(
    detail,
  );
  const permission = result.code !== 0 && /EACCES|permission denied/i.test(detail);
  const lines = [summary];
  if (detail) {
    lines.push(detail);
  }
  if (permission) {
    lines.push("AgentBuildKit does not use sudo.");
  }
  if (network) {
    lines.push("This step needs a network connection.");
  }
  return lines.join("\n");
}

module.exports = { failureDetail, installProduct, locateAgentkit, runDoctor };
