const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { askYesNo } = require("../src/prompt");

const root = path.join(__dirname, "..");
const bootstrapper = path.join(root, "bin", "agentbuildkit.js");

test("answering the demo prompt releases stdin for the next process", async () => {
  const events = [];
  const input = new EventEmitter();
  input.resume = () => {
    process.nextTick(() => {
      input.emit("data", Buffer.from("y\n"));
    });
  };
  input.pause = () => {
    events.push("pause");
  };
  input._handle = {
    setBlocking(blocking) {
      events.push(blocking ? "blocking" : "nonblocking");
    },
  };
  const output = { write() {} };
  const accepted = await askYesNo("Run the production harness demo? [Y/n] ", {
    input,
    output,
    interactive: true,
  });
  assert.equal(accepted, true);
  assert.deepEqual(events, ["pause", "blocking"]);
});

test(
  "a fresh install still delivers the approval answer",
  { skip: process.platform === "win32", timeout: 20_000 },
  async () => {
    const fixture = writeFixtures();
    try {
      const early = await runBootstrap(fixture, { installed: false, mode: "early" });
      assert.match(early, /Installing AgentBuildKit with uv\./);
      assert.match(early, /RESULT 'y'/);

      const prompted = await runBootstrap(fixture, { installed: false, mode: "yes" });
      assert.match(prompted, /Installing AgentBuildKit with uv\./);
      assert.match(prompted, /RESULT 'y'/);

      const installed = await runBootstrap(fixture, { installed: true, mode: "yes" });
      assert.match(installed, /AgentBuildKit already installed/);
      assert.doesNotMatch(installed, /Installing AgentBuildKit with uv\./);
      assert.match(installed, /RESULT 'y'/);

      const rejected = await runBootstrap(fixture, { installed: true, mode: "no" });
      assert.match(rejected, /RESULT 'n'/);
      const empty = await runBootstrap(fixture, { installed: true, mode: "empty" });
      assert.match(empty, /RESULT ''/);
    } finally {
      fs.rmSync(fixture, { recursive: true, force: true });
    }
  },
);

function writeFixtures() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "abk-handoff-"));
  const bin = path.join(directory, "bin");
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, "uv"), uvScript(), { mode: 0o755 });
  fs.writeFileSync(path.join(bin, "agentkit"), agentkitScript(), { mode: 0o755 });
  fs.writeFileSync(path.join(directory, "drive.py"), driverScript());
  return directory;
}

function uvScript() {
  return `#!/bin/sh
if [ "$1" = "tool" ] && [ "$2" = "list" ]; then
  if [ -n "\${ABK_INSTALLED:-}" ]; then
    echo "agentbuildkit v0.1.0"
  else
    echo "No tools installed"
  fi
  exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
  sleep 0.2
  exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then
  echo "$ABK_BIN"
  exit 0
fi
echo "unexpected: $*" >&2
exit 1
`;
}

function agentkitScript() {
  return `#!/bin/sh
if [ "$1" = "doctor" ]; then
  echo "Status: ready for offline demo"
  exit 0
fi
if [ "$1" = "demo" ]; then
  echo "DEMO_STARTED"
  sleep 0.4
  exec "$ABK_PY" -c 'import sys
print("calling-input", flush=True)
try:
    line = input("Approve? [y/N]: ")
except EOFError:
    print("RESULT EOF", flush=True)
    raise SystemExit(0)
print("RESULT", repr(line), flush=True)
'
fi
echo "unexpected: $*" >&2
exit 1
`;
}

function driverScript() {
  return `import os, pty, select, sys, time

mode = os.environ["ABK_MODE"]
node = os.environ["ABK_NODE"]
bootstrapper = os.environ["ABK_BOOT"]
fixture = os.environ["ABK_FIXTURE"]
env = os.environ.copy()
env["PATH"] = os.path.join(fixture, "bin") + os.pathsep + env.get("PATH", "")
env["ABK_BIN"] = os.path.join(fixture, "bin")
env["ABK_PY"] = sys.executable
if os.environ.get("ABK_INSTALLED") == "1":
    env["ABK_INSTALLED"] = "1"
else:
    env.pop("ABK_INSTALLED", None)

pid, fd = pty.fork()
if pid == 0:
    os.chdir(fixture)
    os.execvpe(node, [node, bootstrapper], env)

def alive(child):
    try:
        os.kill(child, 0)
    except OSError:
        return False
    return True

buf = b""
sent_demo = False
sent_approval = False
deadline = time.time() + 8
while time.time() < deadline:
    ready, _, _ = select.select([fd], [], [], 0.05)
    if not ready:
        if not alive(pid):
            break
        continue
    try:
        chunk = os.read(fd, 4096)
    except OSError:
        break
    if not chunk:
        break
    buf += chunk
    if not sent_demo and b"Run the production harness demo?" in buf:
        sent_demo = True
        os.write(fd, b"y\\r")
    if mode == "early" and not sent_approval and b"DEMO_STARTED" in buf:
        sent_approval = True
        os.write(fd, b"y\\r")
    if mode != "early" and not sent_approval and b"Approve? [y/N]:" in buf:
        sent_approval = True
        os.write(fd, b"y\\r" if mode == "yes" else b"n\\r" if mode == "no" else b"\\r")
if alive(pid):
    try:
        os.killpg(pid, 15)
    except OSError:
        pass
    time.sleep(0.1)
    if alive(pid):
        try:
            os.killpg(pid, 9)
        except OSError:
            pass
try:
    os.waitpid(pid, 0)
except ChildProcessError:
    pass
os.close(fd)
sys.stdout.buffer.write(buf)
`;
}

function runBootstrap(fixture, { installed, mode }) {
  return new Promise((resolve, reject) => {
    const child = spawn("python3", [path.join(fixture, "drive.py")], {
      env: {
        ...process.env,
        ABK_MODE: mode,
        ABK_NODE: process.execPath,
        ABK_BOOT: bootstrapper,
        ABK_FIXTURE: fixture,
        ABK_INSTALLED: installed ? "1" : "0",
      },
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`handoff driver exited ${code}\n${stdout}\n${stderr}`));
        return;
      }
      resolve(stdout);
    });
  });
}
