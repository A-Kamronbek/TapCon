/* Marking a notification read, without losing your place.
 *
 * The row is a real form and works with no JavaScript at all - it POSTs and
 * the page reloads. That reload closes the bell panel, which is jarring when
 * you are reading through several items, so inside the panel this sends the
 * same POST in the background and updates the row and the badge in place.
 *
 * Only inside the panel. On the notifications page a reload costs nothing and
 * keeps every count on the page honest, including the "N unread" heading whose
 * plural rules belong to the server, not here.
 *
 * Anything unexpected falls back to submitting the form normally.
 */
(function () {
  "use strict";

  function badge() {
    return document.querySelector(".bell-count");
  }

  function setUnreadCount(n) {
    var el = badge();
    if (n > 0) {
      if (el) el.textContent = n;
      return;
    }
    if (el) el.remove();
    document.querySelectorAll(".bell-read").forEach(function (form) {
      form.remove();
    });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target.closest(".bell-panel .notif-form");
    if (!form || form.dataset.busy === "1") return;

    event.preventDefault();
    form.dataset.busy = "1";

    fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
    })
      .then(function (response) {
        if (!response.ok) throw new Error(response.status);
        return response.json();
      })
      .then(function (data) {
        var row = form.closest(".bell-item");
        if (row) row.classList.remove("unread");
        setUnreadCount(data.unread);
        if (data.url) window.location.href = data.url;
        form.dataset.busy = "";
      })
      .catch(function () {
        // Network or server trouble: let the browser do it the plain way.
        form.dataset.busy = "";
        form.submit();
      });
  });
})();
