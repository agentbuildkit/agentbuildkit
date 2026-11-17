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
const DASHBOARD_PROMPT = "Open the local dashboard? It does not call a model. [Y/n] ";

function parseArgs(argv, env = process.env) {
  const options = {
    help: false,
    noDemo: false,
    noDashboard: false,
    stateDirectory: "",
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
    } else if (arg === "--no-dashboard") {
      options.noDashboard = true;
    } else if (arg === "--state-directory") {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) {
        throw new BootstrapError("--state-directory needs a directory path.");
      }
      options.stateDirectory = value;
      index += 1;
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
    "Install the Python harness and the first-party templates.",
    "The upcoming 0.2.0 release is not the published npm 0.0.2 package.",
    "",
    "Usage:",
    "  npx agentbuildkit",
    "  npx agentbuildkit --no-demo --no-dashboard",
    "  npx agentbuildkit --state-directory /ABSOLUTE/PATH/agentbuildkit-state",
    "  npx agentbuildkit --source local",
    "",
    "Options:",
    "  --help            Show this help",
    "  --no-demo         Do not run agentkit demo",
    "  --no-dashboard    Do not open the local dashboard",
    "  --state-directory Directory kept for the dashboard when you open it",
    "  --upgrade         Reinstall the harness and templates",
    "  --install-uv      Allow Astral's uv installer when uv is missing",
    "  --source          package (default), local, or a repository path",
    "  --verbose         Show error details",
    "",
    "Environment:",
    "  AGENTBUILDKIT_SOURCE    Same values as --source",
    "",
    "The default source installs agentbuildkit>=0.2.0,<0.3.0 and",
    "agentbuildkit-templates>=0.2.0,<0.3.0. It does not install Composio or Foundry.",
    "The published npm 0.0.2 package installs the harness only.",
    "A harness-only tool install is upgraded with the templates package.",
    "--source local installs this checkout and the examples/ templates package.",
    "",
    "An interactive terminal explains the state directory and can open the dashboard.",
    "The dashboard stays in this terminal. Press Ctrl+C to stop it.",
    "It does not call a model. A non-interactive run does not open it.",
    "",
    "After installation:",
    "  agentkit doctor",
    "  agentkit demo",
    "  agentkit dashboard --state-directory /ABSOLUTE/PATH/agentbuildkit-state",
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
  const locate = () =>
    locateAgentkit({
      uv: uv.command,
      run: options.run,
      find: options.findExecutable,
    });
  const installed = await installProduct({
    uv: uv.command,
    source,
    upgrade: options.upgrade,
    run: options.run,
    log,
    locate,
  });
  log(installed.already ? "✓ AgentBuildKit already installed" : "✓ AgentBuildKit installed");

  const agentkit = await locate();
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
  log("");
  for (const line of stateDirectoryLines(options.stateDirectory)) {
    log(line);
  }
  await offerDashboard(options, agentkit.command);

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

function stateDirectoryLines(stateDirectory) {
  const lines = [
    "The dashboard keeps teams, drafts, approvals, and the audit log in one directory.",
    "A --state-directory path is kept after the process stops.",
    "Without that flag, a .env file in the current directory wins over APP_STATE_DIRECTORY in the shell.",
    "With none of those, the state lasts only until the dashboard stops.",
    "Starting the dashboard does not call a model.",
  ];
  if (stateDirectory) {
    lines.push(`This launch will use ${stateDirectory}.`);
  }
  return lines;
}

async function offerDashboard(options, agentkit) {
  const log = options.log;
  if (options.noDashboard) {
    log("Dashboard skipped. Run `agentkit dashboard --state-directory DIRECTORY` when you want it.");
    return;
  }
  if (!options.interactive) {
    log("Dashboard skipped because this terminal is not interactive.");
    log("Run `agentkit dashboard --state-directory DIRECTORY` when you want it.");
    return;
  }
  const accepted = await options.ask(DASHBOARD_PROMPT, {
    input: options.input,
    output: options.output,
    interactive: true,
  });
  if (!accepted) {
    log("Dashboard skipped. Run `agentkit dashboard --state-directory DIRECTORY` when you want it.");
    return;
  }
  log("The dashboard runs in this terminal. Press Ctrl+C to stop it.");
  releaseInput(options.input);
  const args = ["dashboard"];
  if (options.stateDirectory) {
    args.push("--state-directory", options.stateDirectory);
  }
  const dashboard = await options.run(agentkit, args, { stdio: "inherit" });
  if (dashboard.code === 0 || dashboard.code === 130) {
    log("Dashboard stopped.");
    return;
  }
  throw new BootstrapError("agentkit dashboard stopped. Run `agentkit dashboard` to see the result.");
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

module.exports = { DASHBOARD_PROMPT, DEMO_PROMPT, bootstrap, helpText, main, parseArgs };
