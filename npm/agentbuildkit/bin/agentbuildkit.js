#!/usr/bin/env node

const major = Number(process.versions.node.split(".")[0]);
if (!Number.isInteger(major) || major < 18) {
  process.stderr.write(
    `Node.js ${process.versions.node} is not supported. Install Node.js 18 or newer, then run this command again.\n`,
  );
  process.exit(1);
}

const { main } = require("../src/cli");

main(process.argv.slice(2)).then((code) => {
  process.exit(code);
});
