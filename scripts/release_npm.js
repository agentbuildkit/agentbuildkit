const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");

const VERSION = "0.0.4";

function decide(localSha, remoteSha) {
  if (!remoteSha) {
    return "absent";
  }
  if (localSha === remoteSha) {
    return "match";
  }
  return "conflict";
}

function tarballShasum(file) {
  return createHash("sha1").update(fs.readFileSync(file)).digest("hex");
}

function writeDecision(decision) {
  process.stdout.write(`${decision}\n`);
  if (process.env.GITHUB_OUTPUT) {
    fs.appendFileSync(process.env.GITHUB_OUTPUT, `decision=${decision}\n`);
  }
}

function remoteShasum() {
  try {
    return execFileSync("npm", ["view", `agentbuildkit@${VERSION}`, "dist.shasum", "--json"], {
      encoding: "utf8",
    })
      .trim()
      .replaceAll('"', "");
  } catch (error) {
    const detail = `${error.stdout || ""}\n${error.stderr || ""}`;
    if (/E404|404/.test(detail)) {
      return "";
    }
    throw error;
  }
}

function main(argv) {
  const args = argv.slice(2);
  const requireMatch = args.includes("--require-match");
  const file = args.find((item) => !item.startsWith("--"));
  if (!file) {
    throw new Error("Pass the validated tarball. This command does not pack a new one.");
  }
  if (!file.endsWith(`agentbuildkit-${VERSION}.tgz`)) {
    throw new Error(`Expected agentbuildkit-${VERSION}.tgz.`);
  }
  const decision = decide(tarballShasum(file), remoteShasum());
  if (decision === "conflict") {
    throw new Error(
      `npm ${VERSION} is already published and does not match the validated tarball. Stop for review.`,
    );
  }
  if (requireMatch && decision !== "match") {
    throw new Error("The published npm tarball is not the validated candidate.");
  }
  writeDecision(decision);
}

module.exports = { VERSION, decide, tarballShasum };

if (require.main === module) {
  main(process.argv);
}
