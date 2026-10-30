"use strict";

const { BootstrapError } = require("./errors");

const SUPPORTED = {
  "darwin:arm64": "macOS Apple Silicon",
  "darwin:x64": "macOS Intel",
  "linux:x64": "Linux x64",
  "linux:arm64": "Linux arm64",
};

function assertNodeVersion(version) {
  const major = Number(String(version).split(".")[0]);
  if (!Number.isInteger(major) || major < 18) {
    throw new BootstrapError(
      `Node.js ${version} is not supported. Install Node.js 18 or newer, then run this command again.`,
    );
  }
}

function detectPlatform(platform, arch) {
  const label = SUPPORTED[`${platform}:${arch}`];
  if (!label) {
    throw new BootstrapError(
      `Unsupported system: ${platform} ${arch}. AgentBuildKit currently supports macOS (Apple Silicon and Intel) and Linux (x64 and arm64).`,
    );
  }
  return { platform, arch, label };
}

module.exports = { assertNodeVersion, detectPlatform };
