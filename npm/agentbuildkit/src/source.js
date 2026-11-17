"use strict";

const fs = require("node:fs");
const path = require("node:path");

const { BootstrapError } = require("./errors");

const PACKAGE_NAME = "agentbuildkit";
const HARNESS_SPEC = "agentbuildkit>=0.2.0,<0.3.0";
const TEMPLATES_SPEC = "agentbuildkit-templates>=0.2.0,<0.3.0";

function installArguments(source, { upgrade = false } = {}) {
  const args = ["tool", "install", "--python", "3.12", "--managed-python"];
  if (upgrade) {
    args.push("--force");
  }
  if (source.kind === "local") {
    args.push("--with", path.join(source.path, "examples"));
    args.push(source.path);
  } else {
    args.push("--with", TEMPLATES_SPEC);
    args.push(HARNESS_SPEC);
  }
  return args;
}

function templatesReady(doctorOutput) {
  const line = String(doctorOutput)
    .split("\n")
    .find((item) => item.trimStart().startsWith("Templates"));
  if (!line || line.includes("not installed")) {
    return false;
  }
  return line.includes("installed");
}

function toolIsInstalled(listOutput, name = PACKAGE_NAME) {
  return String(listOutput)
    .split("\n")
    .some((line) => line === name || line.startsWith(`${name} `));
}

function resolveSource(spec, { cwd, scriptDir, readFile = fs.readFileSync, exists = fs.existsSync } = {}) {
  const requested = spec || "package";
  if (requested === "package" || requested === "pypi") {
    return { kind: "package", name: PACKAGE_NAME };
  }
  if (requested === "local") {
    const found = findRepository(cwd, scriptDir, { readFile, exists });
    if (!found) {
      throw new BootstrapError(
        "No AgentBuildKit repository was found. Run this command from the repository, or pass --source with the repository path.",
      );
    }
    return { kind: "local", path: found };
  }
  const repository = path.resolve(cwd || process.cwd(), requested);
  if (!exists(path.join(repository, "pyproject.toml"))) {
    throw new BootstrapError(`AgentBuildKit source was not found at ${repository}`);
  }
  return { kind: "local", path: repository };
}

function findRepository(cwd, scriptDir, { readFile, exists }) {
  const starts = [cwd, scriptDir].filter((value) => typeof value === "string" && value !== "");
  for (const start of starts) {
    const found = walkForRepository(start, { readFile, exists });
    if (found) {
      return found;
    }
  }
  return null;
}

function walkForRepository(start, { readFile, exists }) {
  let current = path.resolve(start);
  const root = path.parse(current).root;
  while (true) {
    const manifest = path.join(current, "pyproject.toml");
    if (exists(manifest)) {
      const text = readFile(manifest, "utf8");
      if (/^name\s*=\s*"agentbuildkit"/m.test(text)) {
        return current;
      }
    }
    if (current === root) {
      return null;
    }
    current = path.dirname(current);
  }
}

module.exports = {
  HARNESS_SPEC,
  PACKAGE_NAME,
  TEMPLATES_SPEC,
  findRepository,
  installArguments,
  resolveSource,
  templatesReady,
  toolIsInstalled,
};
