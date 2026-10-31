"use strict";

const os = require("node:os");
const path = require("node:path");

const { findExecutable, runCommand } = require("./commands");
const { BootstrapError } = require("./errors");
const { installProduct, locateAgentkit, runDoctor } = require("./installer");
const { assertNodeVersion, detectPlatform } = require("./platform");
const { askYesNo, releaseInput } = require("./prompt");
const { resolveSource } = require("./source");
const { ensureUv, installUv } = require("./uv");

const DEMO_PROMPT = "Run the production harness demo? [Y/n] ";

function parseArgs(argv, env = process.env) {
  const options = {
    help: false,
    noDemo: false,
    verbose: false,
    upgrade: false,
    installMissingUv: false,
    source: env.AGENTBUILDKIT_SOURCE || "package",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--help" || arg === "-h") {
      options.help = true;
    } else if (arg === "--no-demo") {
      options.noDemo = true;
    } else if (arg === "--verbose") {
      options.verbose = true;
    } else if (arg === "--upgrade") {
      options.upgrade = true;
    } else if (arg === "--install-uv") {
      options.installMissingUv = true;
    } else if (arg === "--source") {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) {
        throw new BootstrapError("--source needs a value: local, package, or a repository path.");
      }
      options.source = value;
      index += 1;
    } else {
      throw new BootstrapError(`Unknown option: ${arg}. Run agentbuildkit --help.`);
    }
  }
  return options;
}

function helpText() {
  return [
    "AgentBuildKit",
    "Build agents. Not demos.",
    "",
    "Install the Python AgentBuildKit CLI into a uv-managed environment.",
    "",
    "Usage:",
    "  npx agentbuildkit",
    "  npx agentbuildkit --no-demo",
    "  npx agentbuildkit --source local",
    "",
    "Options:",
    "  --help       Show this help",
    "  --no-demo    Do not run agentkit demo",
    "  --upgrade     Reinstall from the selected source",
    "  --install-uv Allow Astral's uv installer when uv is missing",
    "  --source     package (default), local, or a repository path",
    "  --verbose    Show error details",
    "",
    "Environment:",
    "  AGENTBUILDKIT_SOURCE    Same values as --source",
    "",
    "The default source installs the agentbuildkit Python package with uv.",
    "Before that package is on PyPI, run this from the repository with --source local.",
    "",
    "After installation:",
    "  agentkit doctor",
    "  agentkit demo",
    "  uv tool uninstall agentbuildkit",
    "",
  ].join("\n");
}

async function bootstrap(options) {
  const log = options.log;
  log("AgentBuildKit");
  log("Build agents. Not demos.");
  log("");
  log("Checking your system...");
  log("");
  assertNodeVersion(options.nodeVersion || process.versions.node);
  const system = detectPlatform(
    options.platform || os.platform(),
    options.arch || os.arch(),
  );
  log(`✓ ${system.label} detected`);

  const uv = await ensureUv({
    find: options.findExecutable,
    install: options.installUv,
    log,
    ask: options.ask,
    input: options.input,
    output: options.output,
    interactive: options.interactive,
    installMissingUv: options.installMissingUv,
  });
  log(uv.installed ? "✓ uv installed" : "✓ uv available");

  const source = resolveSource(options.source, {
    cwd: options.cwd,
    scriptDir: options.scriptDir,
    readFile: options.readFile,
    exists: options.exists,
  });
  const installed = await installProduct({
    uv: uv.command,
    source,
    upgrade: options.upgrade,
    run: options.run,
    log,
  });
  log(installed.already ? "✓ AgentBuildKit already installed" : "✓ AgentBuildKit installed");

  const agentkit = await locateAgentkit({
    uv: uv.command,
    run: options.run,
    find: options.findExecutable,
  });
  if (!agentkit.onPath) {
    log("");
    log(`agentkit is installed at ${agentkit.command}`);
    log("That directory is not on PATH. Add it with:");
    log(`  export PATH="${agentkit.binDir}:$PATH"`);
  }

  const doctor = await runDoctor(agentkit.command, options.run);
  if (doctor.stdout.trim() !== "") {
    log("");
    log(doctor.stdout.trimEnd());
  }
  log("✓ Runtime healthy");
  log("");
  log("AgentBuildKit is ready.");

  if (options.noDemo) {
    log("Demo skipped. Run `agentkit demo` when you want it.");
    return 0;
  }
  if (!options.interactive) {
    log("Demo skipped because this terminal is not interactive.");
    log("Run `agentkit demo` when you want it.");
    return 0;
  }
  const accepted = await options.ask(DEMO_PROMPT, {
    input: options.input,
    output: options.output,
    interactive: true,
  });
  if (!accepted) {
    log("Demo skipped. Run `agentkit demo` when you want it.");
    return 0;
  }
  releaseInput(options.input);
  const demo = await options.run(agentkit.command, ["demo"], { stdio: "inherit" });
  if (demo.code !== 0) {
    throw new BootstrapError("agentkit demo failed. Run `agentkit demo` to see the result.");
  }
  return 0;
}

function runtimeDefaults() {
  return {
    log: (line) => {
      process.stdout.write(`${line}\n`);
    },
    run: runCommand,
    findExecutable: (name) => findExecutable(name),
    installUv: () => installUv(),
    ask: askYesNo,
    input: process.stdin,
    output: process.stdout,
    interactive: Boolean(process.stdin.isTTY),
    cwd: process.cwd(),
    scriptDir: path.join(__dirname, ".."),
  };
}

async function main(argv, overrides = {}) {
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    writeError(error, overrides.verbose === true);
    return 1;
  }
  if (options.help) {
    const log = overrides.log || runtimeDefaults().log;
    log(helpText());
    return 0;
  }
  try {
    return await bootstrap({ ...runtimeDefaults(), ...overrides, ...options });
  } catch (error) {
    writeError(error, options.verbose);
    return 1;
  }
}

function writeError(error, verbose) {
  if (error instanceof BootstrapError) {
    process.stderr.write(`${error.message}\n`);
    return;
  }
  process.stderr.write("AgentBuildKit could not finish.\n");
  if (verbose && error instanceof Error && error.stack) {
    process.stderr.write(`${error.stack}\n`);
    return;
  }
  process.stderr.write("Run again with --verbose for details.\n");
}

module.exports = { DEMO_PROMPT, bootstrap, helpText, main, parseArgs };
