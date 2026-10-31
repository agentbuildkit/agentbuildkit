"use strict";

function askYesNo(promptText, { input, output, interactive }) {
  if (!interactive) {
    return Promise.resolve(false);
  }
  output.write(promptText);
  return new Promise((resolve) => {
    let answer = "";
    let settled = false;
    const finish = (value) => {
      if (settled) {
        return;
      }
      settled = true;
      input.removeListener("data", onData);
      input.removeListener("end", onEnd);
      // Drop the flowing read before the caller inherits stdin. Node does not
      // pause an inherited descriptor, so the next line would be discarded.
      releaseInput(input);
      resolve(value);
    };
    const onData = (chunk) => {
      answer += chunk;
      const newline = answer.indexOf("\n");
      if (newline === -1) {
        return;
      }
      finish(isYes(answer.slice(0, newline)));
    };
    const onEnd = () => finish(false);
    input.on("data", onData);
    input.on("end", onEnd);
    if (typeof input.resume === "function") {
      input.resume();
    }
  });
}

function releaseInput(input) {
  if (input && typeof input.pause === "function") {
    input.pause();
  }
  const handle = input && input._handle;
  if (handle && typeof handle.setBlocking === "function") {
    handle.setBlocking(true);
  }
}

function isYes(answer) {
  const value = String(answer).trim().toLowerCase();
  return value === "" || value === "y" || value === "yes";
}

module.exports = { askYesNo, isYes, releaseInput };
