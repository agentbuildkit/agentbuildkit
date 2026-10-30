const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const test = require("node:test");

const { bootstrap, DEMO_PROMPT } = require("../src/cli");
const { UV_INSTALL_PROMPT } = require("../src/uv");
const { BootstrapError } = require("../src/errors");
const { askYesNo } = require("../src/prompt");
const { installArguments, resolveSource } = require("../src/source");

const secret = "sk-bootstrap-secret";
const repository = "/Users/me/My Repo";
const binDir = "/Users/me/Tool Bin";

test("local and package install commands keep paths as single arguments", () => {
  assert.deepEqual(installArguments({ kind: "package", name: "agentbuildkit" }), [
    "tool",
    "install",
    "--python",
    "3.12",
    "--managed-python",
    "agentbuildkit",
  ]);
  assert.deepEqual(
    installArguments({ kind: "local", path: repository }, { upgrade: true }),
    ["tool", "install", "--python", "3.12", "--managed-python", "--force", repository],
  );
});

test("local source resolves the repository that declares agentbuildkit", () => {
  const manifest = pathJoin(repository, "pyproject.toml");
  const files = new Set([manifest]);
  const source = resolveSource("local", {
    cwd: pathJoin(repository, "npm", "agentbuildkit"),
    scriptDir: pathJoin(repository, "npm", "agentbuildkit"),
    exists: (file) => files.has(file),
    readFile: () => 'name = "agentbuildkit"\n',
  });
  assert.equal(source.kind, "local");
  assert.equal(source.path, repository);
});

test("package source does not point at a local directory", () => {
  const source = resolveSource("package", { cwd: repository, scriptDir: repository });
  assert.deepEqual(source, { kind: "package", name: "agentbuildkit" });
  assert.deepEqual(installArguments(source).at(-1), "agentbuildkit");
});

test("uv that is already installed is reused", async () => {
  const harness = createHarness();
  const code = await harness.run();
  assert.equal(code, 0);
  assert.equal(harness.installedUv, false);
  assert.equal(harness.lines.includes("✓ uv available"), true);
  assert.equal(callsNamed(harness, "tool", "install").length, 1);
});

test("missing uv is installed before AgentBuildKit", async () => {
  let seen = false;
  const harness = createHarness({
    installMissingUv: true,
    find(name) {
      if (name === "uv") {
        return seen ? "/home/dev/.local/bin/uv" : null;
      }
      return "/home/dev/.local/bin/agentkit";
    },
    async installUv() {
      seen = true;
      return { code: 0, stdout: "", stderr: "" };
    },
  });
  const code = await harness.run();
  assert.equal(code, 0);
  assert.equal(seen, true);
  const notice = harness.lines.findIndex((line) => line.includes("https://astral.sh/uv/install.sh"));
  const ready = harness.lines.findIndex((line) => line === "✓ uv installed");
  assert.equal(notice > -1 && notice < ready, true);
});

test("uv installation failure stops before AgentBuildKit is installed", async () => {
  const harness = createHarness({
    installMissingUv: true,
    find() {
      return null;
    },
    async installUv() {
      return { code: 1, stdout: "", stderr: "could not resolve host" };
    },
  });
  await assert.rejects(harness.run(), /uv could not be installed/);
  assert.equal(harness.calls.length, 0);
});

test("an interactive yes installs missing uv", async () => {
  let seen = false;
  let prompt = "";
  const harness = createHarness({
    noDemo: true,
    interactive: true,
    find(name) {
      if (name === "uv") {
        return seen ? "/home/dev/.local/bin/uv" : null;
      }
      return null;
    },
    async installUv() {
      seen = true;
      return { code: 0, stdout: "", stderr: "" };
    },
    ask(value) {
      prompt = value;
      return true;
    },
  });
  const code = await harness.run();
  assert.equal(code, 0);
  assert.equal(seen, true);
  assert.equal(prompt, UV_INSTALL_PROMPT);
  assert.equal(harness.lines.includes("uv is required but isn't installed."), true);
  assert.equal(
    harness.lines.includes("AgentBuildKit can install uv using Astral's official installer."),
    true,
  );
});

test("an interactive no does not install uv", async () => {
  let called = false;
  const harness = createHarness({
    interactive: true,
    find() {
      return null;
    },
    async installUv() {
      called = true;
      return { code: 0, stdout: "", stderr: "" };
    },
    ask() {
      return false;
    },
  });
  await assert.rejects(harness.run(), /uv was not installed/);
  assert.equal(called, false);
  assert.equal(harness.calls.length, 0);
});

test("a non-interactive run does not install uv without --install-uv", async () => {
  let called = false;
  const harness = createHarness({
    interactive: false,
    find() {
      return null;
    },
    async installUv() {
      called = true;
      return { code: 0, stdout: "", stderr: "" };
    },
  });
  await assert.rejects(harness.run(), /--install-uv/);
  assert.equal(called, false);
  assert.equal(harness.calls.length, 0);
});

test("doctor success is reported from the agentkit command", async () => {
  const harness = createHarness();
  await harness.run();
  const doctor = harness.calls.find((call) => call.args[0] === "doctor");
  assert.equal(doctor.command, pathJoin(binDir, "agentkit"));
  assert.deepEqual(doctor.args, ["doctor"]);
  assert.equal(harness.lines.includes("✓ Runtime healthy"), true);
});

test("doctor failure is returned to the user", async () => {
  const harness = createHarness({
    doctor: { code: 1, stdout: "", stderr: "Python check failed\n" },
  });
  await assert.rejects(harness.run(), /agentkit doctor failed/);
  assert.equal(harness.lines.includes("✓ Runtime healthy"), false);
});

test("an interactive yes runs agentkit demo", async () => {
  let prompt = "";
  const harness = createHarness({
    interactive: true,
    ask(value) {
      prompt = value;
      return true;
    },
  });
  await harness.run();
  assert.equal(prompt, DEMO_PROMPT);
  const demo = harness.calls.find((call) => call.args[0] === "demo");
  assert.deepEqual(demo.args, ["demo"]);
  assert.equal(demo.stdio, "inherit");
});

test("an interactive no does not run the demo", async () => {
  const harness = createHarness({
    interactive: true,
    ask() {
      return false;
    },
  });
  await harness.run();
  assert.equal(
    harness.calls.some((call) => call.args[0] === "demo"),
    false,
  );
  assert.equal(harness.lines.includes("Demo skipped. Run `agentkit demo` when you want it."), true);
});

test("--no-demo skips the prompt", async () => {
  let asked = false;
  const harness = createHarness({
    noDemo: true,
    interactive: true,
    ask() {
      asked = true;
      return true;
    },
  });
  await harness.run();
  assert.equal(asked, false);
  assert.equal(
    harness.calls.some((call) => call.args[0] === "demo"),
    false,
  );
});

test("a non-interactive terminal does not wait for input", async () => {
  let asked = false;
  const harness = createHarness({
    interactive: false,
    ask() {
      asked = true;
      return true;
    },
  });
  await harness.run();
  assert.equal(asked, false);
  assert.equal(
    harness.lines.includes("Demo skipped because this terminal is not interactive."),
    true,
  );
});

test("install arguments preserve a repository path that contains spaces", async () => {
  const harness = createHarness();
  await harness.run();
  const install = callsNamed(harness, "tool", "install")[0];
  assert.equal(install.args.at(-1), repository);
  assert.equal(install.args.includes("/Users/me/My"), false);
  const doctor = harness.calls.find((call) => call.args[0] === "doctor");
  assert.equal(doctor.command, "/Users/me/Tool Bin/agentkit");
});

test("a second run does not install AgentBuildKit again", async () => {
  const harness = createHarness();
  await harness.run();
  const firstInstalls = callsNamed(harness, "tool", "install").length;
  harness.lines.length = 0;
  harness.responses.length = 0;
  harness.responses.push(
    { code: 0, stdout: "agentbuildkit v0.1.0\n- agentkit\n", stderr: "" },
    { code: 0, stdout: `${binDir}\n`, stderr: "" },
    { code: 0, stdout: "AgentBuildKit Doctor\nStatus: ready for offline demo\n", stderr: "" },
  );
  await harness.run();
  assert.equal(callsNamed(harness, "tool", "install").length, firstInstalls);
  assert.equal(harness.lines.includes("✓ AgentBuildKit already installed"), true);
});

test("logs do not include secret environment values", async () => {
  const previousOpenAI = process.env.OPENAI_API_KEY;
  const previousAnthropic = process.env.ANTHROPIC_API_KEY;
  process.env.OPENAI_API_KEY = secret;
  process.env.ANTHROPIC_API_KEY = secret;
  try {
    const harness = createHarness();
    await harness.run();
    const text = harness.lines.join("\n");
    assert.equal(text.includes(secret), false);
    assert.equal(JSON.stringify(harness.calls).includes(secret), false);
  } finally {
    restoreEnv("OPENAI_API_KEY", previousOpenAI);
    restoreEnv("ANTHROPIC_API_KEY", previousAnthropic);
  }
});

test("the demo prompt defaults to yes and treats a closed stdin as no", async () => {
  const output = { text: "", write(value) { this.text += value; } };
  const accepted = await askYesNo(DEMO_PROMPT, {
    input: scriptedInput("\n"),
    output,
    interactive: true,
  });
  const rejected = await askYesNo(DEMO_PROMPT, {
    input: scriptedInput("n\n"),
    output,
    interactive: true,
  });
  const closed = await askYesNo(DEMO_PROMPT, {
    input: scriptedInput(null),
    output,
    interactive: true,
  });
  assert.equal(accepted, true);
  assert.equal(rejected, false);
  assert.equal(closed, false);
  assert.equal(output.text.includes(secret), false);
});

function createHarness(overrides = {}) {
  const calls = [];
  const lines = [];
  const responses = [
    { code: 0, stdout: "No tools installed\n", stderr: "" },
    { code: 0, stdout: "", stderr: "" },
    { code: 0, stdout: `${binDir}\n`, stderr: "" },
    overrides.doctor || { code: 0, stdout: "AgentBuildKit Doctor\nStatus: ready for offline demo\n", stderr: "" },
    { code: 0, stdout: "", stderr: "" },
  ];
  const harness = {
    calls,
    lines,
    responses,
    installedUv: false,
    async run() {
      return bootstrap({
        log: (line) => lines.push(line),
        platform: "darwin",
        arch: "arm64",
        nodeVersion: "20.11.0",
        source: "local",
        cwd: repository,
        scriptDir: repository,
        noDemo: overrides.noDemo === true,
        upgrade: false,
        installMissingUv: overrides.installMissingUv === true,
        interactive: overrides.interactive === true,
        readFile: () => 'name = "agentbuildkit"\n',
        exists: (file) => file === pathJoin(repository, "pyproject.toml"),
        findExecutable: overrides.find || ((name) => (name === "uv" ? "/usr/local/bin/uv" : null)),
        installUv: overrides.installUv || (async () => {
          throw new Error("uv install should not run");
        }),
        ask: overrides.ask || (async () => false),
        input: scriptedInput("\n"),
        output: { write() {} },
        run(command, args, options = {}) {
          calls.push({ command, args, stdio: options.stdio });
          const next = responses.shift();
          if (!next) {
            throw new Error(`unexpected command ${args.join(" ")}`);
          }
          return next;
        },
      });
    },
  };
  return harness;
}

function callsNamed(harness, first, second) {
  return harness.calls.filter((call) => call.args[0] === first && call.args[1] === second);
}

function scriptedInput(answer) {
  const input = new EventEmitter();
  input.resume = () => {
    process.nextTick(() => {
      if (answer === null) {
        input.emit("end");
        return;
      }
      input.emit("data", answer);
    });
  };
  return input;
}

function pathJoin(...parts) {
  return path.join(...parts);
}

function restoreEnv(name, value) {
  if (value === undefined) {
    delete process.env[name];
    return;
  }
  process.env[name] = value;
}

module.exports = { secret };
