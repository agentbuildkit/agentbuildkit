const assert = require("node:assert/strict");
const test = require("node:test");

const { BootstrapError } = require("../src/errors");
const { assertNodeVersion, detectPlatform } = require("../src/platform");

test("supported platforms are named", () => {
  assert.equal(detectPlatform("darwin", "arm64").label, "macOS Apple Silicon");
  assert.equal(detectPlatform("darwin", "x64").label, "macOS Intel");
  assert.equal(detectPlatform("linux", "x64").label, "Linux x64");
  assert.equal(detectPlatform("linux", "arm64").label, "Linux arm64");
});

test("unsupported platforms are rejected", () => {
  assert.throws(() => detectPlatform("win32", "x64"), BootstrapError);
  assert.throws(() => detectPlatform("linux", "ia32"), BootstrapError);
  assert.throws(() => detectPlatform("freebsd", "x64"), BootstrapError);
});

test("node 18 is the minimum supported version", () => {
  assert.doesNotThrow(() => assertNodeVersion("18.0.0"));
  assert.doesNotThrow(() => assertNodeVersion("22.14.0"));
  assert.throws(() => assertNodeVersion("17.9.0"), /Node\.js 17\.9\.0 is not supported/);
  assert.throws(() => assertNodeVersion("16.20.2"), BootstrapError);
});
