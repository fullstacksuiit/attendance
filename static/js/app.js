// Shared UI behaviour: mobile nav + dismissible alerts.

(function () {
  "use strict";

  var toggle = document.getElementById("navToggle");
  var nav = document.getElementById("mainNav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      nav.classList.toggle("open");
    });
  }

  document.querySelectorAll(".alert-close").forEach(function (button) {
    button.addEventListener("click", function () {
      button.closest(".alert").remove();
    });
  });
})();

/** Read the CSRF token from the nearest form on the page. */
function getCsrfToken() {
  var input = document.querySelector("[name=csrfmiddlewaretoken]");
  return input ? input.value : "";
}
