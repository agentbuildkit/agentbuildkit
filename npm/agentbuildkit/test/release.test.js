const assert = require("node:assert/strict");
const test = require("node:test");

const { decide } = require("../../../scripts/release_npm");

test("an unpublished npm version is absent", () => {
  assert.equal(decide("abc", ""), "absent");
});

test("the same npm shasum can be resumed", () => {
  assert.equal(decide("abc", "abc"), "match");
});

test("a different npm shasum is a conflict", () => {
  assert.equal(decide("local", "remote"), "conflict");
});
