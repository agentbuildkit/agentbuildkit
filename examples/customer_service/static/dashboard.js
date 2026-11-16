// Progressive enhancement only. Every page works without this file.
(function () {
  "use strict";

  // Show a busy state while a POST is in flight. The clicked button's value is
  // copied into a hidden field first, because a disabled button is not submitted.
  // Disabling during the submit event itself cancels the POST in some browsers,
  // which leaves the label stuck on the busy text. Disable on the next turn.
  document.querySelectorAll("form[data-busy-label]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var submitter = event.submitter;
      if (submitter && submitter.name) {
        var carried = document.createElement("input");
        carried.type = "hidden";
        carried.name = submitter.name;
        carried.value = submitter.value;
        form.appendChild(carried);
      }
      form.setAttribute("aria-busy", "true");
      var target = submitter || form.querySelector("button[type=submit]");
      if (target) {
        target.classList.add("is-busy");
        target.textContent = "";
        var spinner = document.createElement("span");
        spinner.className = "spinner";
        spinner.setAttribute("aria-hidden", "true");
        target.appendChild(spinner);
        target.appendChild(document.createTextNode(form.getAttribute("data-busy-label")));
      }
      var status = form.querySelector("[data-busy-status]");
      if (status) {
        status.textContent = status.getAttribute("data-busy-status");
      }
      window.setTimeout(function () {
        form.querySelectorAll("button").forEach(function (button) {
          button.disabled = true;
        });
      }, 0);
    });
  });

  // Examples fill the message field in place instead of reloading the page.
  var message = document.getElementById("message");
  document.querySelectorAll("a[data-example]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      if (!message) {
        return;
      }
      event.preventDefault();
      message.value = link.getAttribute("data-example");
      document.querySelectorAll("a[data-example]").forEach(function (other) {
        other.setAttribute("aria-current", other === link ? "true" : "false");
      });
      message.focus();
    });
  });

  // Escape closes the new-request sheet.
  var sheet = document.querySelector("[data-sheet]");
  if (sheet) {
    sheet.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        var close = sheet.querySelector("[data-close]");
        if (close) {
          window.location.href = close.getAttribute("href");
        }
      }
    });
  }

  // Dismiss a notice on this page only. It is not stored.
  document.querySelectorAll("[data-dismiss]").forEach(function (button) {
    button.addEventListener("click", function () {
      var notice = button.closest("[data-notice]");
      if (notice) {
        notice.hidden = true;
      }
    });
  });

  // Audit filters apply as soon as a choice changes.
  document.querySelectorAll("form[data-autosubmit] select").forEach(function (select) {
    select.addEventListener("change", function () {
      select.form.submit();
    });
  });
})();
