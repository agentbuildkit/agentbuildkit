const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const secret = "sk-bootstrap-secret";

test("package reserves the agentbuildkit command", () => {
  const pkg = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
  assert.equal(pkg.name, "agentbuildkit");
  assert.equal(pkg.version, "0.0.1");
  assert.equal(pkg.description, "One-command bootstrapper for AgentBuildKit");
  assert.equal(pkg.bin.agentbuildkit, "bin/agentbuildkit.js");
  assert.deepEqual(pkg.files, ["bin/agentbuildkit.js", "src"]);
  assert.equal(pkg.engines.node, ">=18");
});

test("help is local and does not print secrets", () => {
  const bin = path.join(root, "bin", "agentbuildkit.js");
  const source = readFileSync(bin, "utf8");
  assert.equal(source.startsWith("#!/usr/bin/env node\n"), true);

  const result = spawnSync(process.execPath, [bin, "--help"], {
    encoding: "utf8",
    env: {
      ...process.env,
      OPENAI_API_KEY: secret,
      ANTHROPIC_API_KEY: secret,
    },
  });
  assert.equal(result.status, 0);
  assert.equal(result.stderr, "");
  assert.match(result.stdout, /npx agentbuildkit/);
  assert.match(result.stdout, /--no-demo/);
  assert.match(result.stdout, /--source/);
  assert.match(result.stdout, /uv tool uninstall agentbuildkit/);
  assert.equal(result.stdout.includes(secret), false);
});
