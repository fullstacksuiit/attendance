// Home-screen install: service worker registration + "Add to Home Screen" prompt.

(function () {
  "use strict";

  var DISMISS_KEY = "attendease.install.dismissed";
  var DISMISS_DAYS = 14;

  // ---------- service worker ----------

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function (err) {
        console.warn("Service worker registration failed:", err);
      });
    });
  }

  // ---------- install banner ----------

  var banner = document.getElementById("installBanner");
  if (!banner) {
    return;
  }

  var acceptBtn = document.getElementById("installAccept");
  var dismissBtn = document.getElementById("installDismiss");
  var hint = document.getElementById("installHint");
  var deferredPrompt = null;

  function isStandalone() {
    return (
      window.matchMedia("(display-mode: standalone)").matches ||
      window.navigator.standalone === true
    );
  }

  function isIos() {
    return (
      /iphone|ipad|ipod/i.test(window.navigator.userAgent) ||
      // iPadOS 13+ reports itself as a Mac; touch points give it away.
      (window.navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
    );
  }

  function recentlyDismissed() {
    try {
      var at = parseInt(localStorage.getItem(DISMISS_KEY) || "", 10);
      return !isNaN(at) && Date.now() - at < DISMISS_DAYS * 86400000;
    } catch (err) {
      return false;
    }
  }

  function hide() {
    banner.hidden = true;
  }

  function show() {
    if (isStandalone() || recentlyDismissed()) {
      return;
    }
    banner.hidden = false;
  }

  if (dismissBtn) {
    dismissBtn.addEventListener("click", function () {
      try {
        localStorage.setItem(DISMISS_KEY, String(Date.now()));
      } catch (err) {
        /* private mode — banner simply returns next visit */
      }
      hide();
    });
  }

  // Chrome / Edge / Samsung on Android: a real install prompt is available.
  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredPrompt = event;
    show();
  });

  if (acceptBtn) {
    acceptBtn.addEventListener("click", function () {
      if (!deferredPrompt) {
        return;
      }
      deferredPrompt.prompt();
      deferredPrompt.userChoice.then(function () {
        deferredPrompt = null;
        hide();
      });
    });
  }

  window.addEventListener("appinstalled", function () {
    deferredPrompt = null;
    try {
      localStorage.setItem(DISMISS_KEY, String(Date.now()));
    } catch (err) {
      /* ignore */
    }
    hide();
  });

  // iOS Safari has no install event — spell out the Share-sheet steps instead.
  if (isIos() && !isStandalone()) {
    if (acceptBtn) {
      acceptBtn.hidden = true;
    }
    if (hint) {
      hint.textContent = "Tap Share, then “Add to Home Screen”.";
    }
    show();
  }
})();
