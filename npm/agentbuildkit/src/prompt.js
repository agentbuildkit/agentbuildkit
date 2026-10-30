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
      resolve(value);
    };
    const onData = (chunk) => {
      answer += chunk;
      if (!answer.includes("\n")) {
        return;
      }
      finish(isYes(answer));
    };
    const onEnd = () => finish(false);
    input.on("data", onData);
    input.on("end", onEnd);
    if (typeof input.resume === "function") {
      input.resume();
    }
  });
}

function isYes(answer) {
  const value = answer.trim().toLowerCase();
  return value === "" || value === "y" || value === "yes";
}

module.exports = { askYesNo, isYes };
