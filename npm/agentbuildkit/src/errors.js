"use strict";

class BootstrapError extends Error {
  constructor(message) {
    super(message);
    this.name = "BootstrapError";
  }
}

module.exports = { BootstrapError };
