/* Waiting for the provider's webhook.
 *
 * When a customer comes back from Payme or Click, the provider's webhook is
 * usually still in flight - the redirect and the callback race, and the
 * redirect normally wins. So the page renders as "waiting" and this asks the
 * server until the payment settles, then reloads once to show the real result.
 *
 * Without this the page still works: it says what is happening and offers a
 * Refresh link. This only saves the tapping.
 *
 * Polling backs off (1s, then a little longer each time, capped at 5s) and
 * gives up after two minutes rather than asking forever on a phone.
 */
(function () {
  "use strict";

  var card = document.querySelector("[data-status-url]");
  if (!card || card.dataset.settled === "1") return;

  var url = card.dataset.statusUrl;
  var delay = 1000;
  var deadline = Date.now() + 120000;

  function stop(message) {
    var note = document.createElement("p");
    note.className = "small muted";
    note.textContent = message;
    card.appendChild(note);
  }

  function poll() {
    if (Date.now() > deadline) {
      stop(card.dataset.timeoutText || "");
      return;
    }

    fetch(url, {
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (response) {
        if (!response.ok) throw new Error(response.status);
        return response.json();
      })
      .then(function (data) {
        if (data.settled) {
          // Reload rather than rewriting the card here: the server owns how a
          // paid, cancelled or failed payment looks, in three languages.
          window.location.reload();
          return;
        }
        delay = Math.min(delay * 1.4, 5000);
        window.setTimeout(poll, delay);
      })
      .catch(function () {
        // A dropped connection is not an answer. Slow down and keep trying
        // until the deadline.
        delay = Math.min(delay * 2, 5000);
        window.setTimeout(poll, delay);
      });
  }

  window.setTimeout(poll, delay);
})();
