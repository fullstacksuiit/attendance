// Attendance sheet: bulk marking, live counter, and save-and-notify via fetch.

(function () {
  "use strict";

  var form = document.getElementById("attendanceForm");
  if (!form) return;

  var rows = Array.prototype.slice.call(form.querySelectorAll("tbody tr[data-employee]"));
  var counter = document.getElementById("selectedCount");
  var saveBtn = document.getElementById("saveBtn");
  var notifyToggle = document.getElementById("notifyToggle");
  var resultBox = document.getElementById("saveResult");

  // Late threshold, in minutes past midnight, mirroring Attendance.minutes_late.
  var lateAfter = toMinutes(form.dataset.start) + parseInt(form.dataset.grace || "0", 10);

  function toMinutes(value) {
    if (!value) return null;
    var parts = value.split(":");
    return parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
  }

  /** Show the "late" flag only for a checked-in employee who arrived past the cutoff. */
  function refreshLateFlag(row) {
    var flag = row.querySelector(".late-flag");
    if (!flag) return;
    var status = statusOf(row);
    var attended = status === "PRESENT" || status === "HALF_DAY";
    var minutes = toMinutes(row.querySelector(".check-in").value);
    flag.hidden = !(attended && minutes !== null && minutes > lateAfter);
  }

  // ---- party-wise orders & collection -----------------------------------

  function money(value) {
    var number = parseFloat(String(value == null ? "" : value).replace(/,/g, ""));
    return isNaN(number) || number < 0 ? 0 : number;
  }

  function format(value) {
    return money(value).toFixed(2);
  }

  function panelFor(row) {
    var panel = document.querySelector('.biz-row[data-for="' + row.dataset.employee + '"]');
    return panel ? panel.querySelector(".biz-panel") : null;
  }

  function addLine(panel, line) {
    var tr = document.createElement("tr");
    tr.innerHTML =
      '<td><input type="text" class="biz-party" maxlength="150" placeholder="Party name"></td>' +
      '<td class="num"><input type="number" class="biz-order" min="0" step="0.01" placeholder="0.00"></td>' +
      '<td class="num"><input type="number" class="biz-collected" min="0" step="0.01" placeholder="0.00"></td>' +
      '<td class="num"><button type="button" class="btn btn-sm btn-ghost biz-remove" aria-label="Remove line">&times;</button></td>';
    panel.querySelector(".biz-lines").appendChild(tr);
    if (line) {
      tr.querySelector(".biz-party").value = line.party_name || "";
      tr.querySelector(".biz-order").value = money(line.order_amount) || "";
      tr.querySelector(".biz-collected").value = money(line.collected_amount) || "";
    }
    return tr;
  }

  /** Read a row's filled-in party lines. Blank/zero rows are dropped. */
  function linesOf(row) {
    var panel = panelFor(row);
    if (!panel) return [];
    return Array.prototype.slice.call(panel.querySelectorAll(".biz-lines tr")).reduce(
      function (acc, tr) {
        var party = tr.querySelector(".biz-party").value.trim();
        var order = money(tr.querySelector(".biz-order").value);
        var collected = money(tr.querySelector(".biz-collected").value);
        if (party && (order > 0 || collected > 0)) {
          acc.push({ party_name: party, order_amount: order, collected_amount: collected });
        }
        return acc;
      },
      []
    );
  }

  function refreshBusiness(row) {
    var panel = panelFor(row);
    if (!panel) return;
    var lines = linesOf(row);
    var orderTotal = lines.reduce(function (sum, l) { return sum + l.order_amount; }, 0);
    var collectedTotal = lines.reduce(function (sum, l) { return sum + l.collected_amount; }, 0);

    panel.querySelector(".biz-order-total").textContent = format(orderTotal);
    panel.querySelector(".biz-collected-total").textContent = format(collectedTotal);

    var label = row.querySelector(".biz-summary");
    label.textContent = lines.length
      ? lines.length + " part" + (lines.length === 1 ? "y" : "ies") + " · ₹" + format(collectedTotal)
      : "Add";
    label.closest("button").classList.toggle("has-lines", lines.length > 0);
  }

  function refreshDayTotals() {
    var orders = 0;
    var orderValue = 0;
    var collected = 0;
    rows.forEach(function (row) {
      if (!statusOf(row)) return;
      linesOf(row).forEach(function (line) {
        if (line.order_amount > 0) orders += 1;
        orderValue += line.order_amount;
        collected += line.collected_amount;
      });
    });
    document.getElementById("dayOrders").textContent = orders;
    document.getElementById("dayOrderValue").textContent = format(orderValue);
    document.getElementById("dayCollected").textContent = format(collected);
  }

  rows.forEach(function (row) {
    var panel = panelFor(row);
    if (!panel) return;
    var saved = [];
    try {
      saved = JSON.parse(panel.dataset.lines || "[]");
    } catch (error) {
      saved = [];
    }
    saved.forEach(function (line) { addLine(panel, line); });
    refreshBusiness(row);
  });

  document.querySelectorAll(".biz-toggle").forEach(function (button) {
    button.addEventListener("click", function () {
      var row = button.closest("tr");
      var panelRow = document.querySelector('.biz-row[data-for="' + row.dataset.employee + '"]');
      panelRow.hidden = !panelRow.hidden;
      if (!panelRow.hidden && !panelRow.querySelector(".biz-lines tr")) {
        addLine(panelRow.querySelector(".biz-panel"));
      }
    });
  });

  document.querySelectorAll(".biz-add").forEach(function (button) {
    button.addEventListener("click", function () {
      addLine(button.closest(".biz-panel"));
    });
  });

  document.addEventListener("click", function (event) {
    if (!event.target.classList.contains("biz-remove")) return;
    var panelRow = event.target.closest(".biz-row");
    event.target.closest("tr").remove();
    var row = form.querySelector('tr[data-employee="' + panelRow.dataset.for + '"]');
    refreshBusiness(row);
    refreshDayTotals();
  });

  form.addEventListener("input", function (event) {
    var panelRow = event.target.closest(".biz-row");
    if (!panelRow) return;
    var row = form.querySelector('tr[data-employee="' + panelRow.dataset.for + '"]');
    refreshBusiness(row);
    refreshDayTotals();
  });

  /** The status this row will save with — an explicit pick beats the schedule default. */
  function statusOf(row) {
    var checked = row.querySelector("input[type=radio]:checked");
    return checked ? checked.value : row.dataset.defaultStatus || "";
  }

  function updateCount() {
    var marked = rows.filter(function (row) {
      return statusOf(row) !== "";
    }).length;
    counter.textContent = marked;
    saveBtn.disabled = marked === 0;
    rows.forEach(refreshLateFlag);
    refreshDayTotals();
  }

  function syncSaveLabel() {
    saveBtn.textContent = notifyToggle.checked ? "Save & Notify" : "Save only";
  }

  form.addEventListener("change", function (event) {
    if (event.target.type === "radio") updateCount();
    if (event.target.type === "time") refreshLateFlag(event.target.closest("tr"));
  });

  var applyTimeBtn = document.getElementById("applyTime");
  if (applyTimeBtn) {
    applyTimeBtn.addEventListener("click", function () {
      var value = document.getElementById("bulkTime").value;
      if (!value) return;
      rows.forEach(function (row) {
        row.querySelector(".check-in").value = value;
        refreshLateFlag(row);
      });
    });
  }

  document.querySelectorAll(".mark-all").forEach(function (button) {
    button.addEventListener("click", function () {
      var status = button.dataset.status;
      rows.forEach(function (row) {
        // Leave week-off and holiday rows alone — overriding those is deliberate.
        if (row.dataset.defaultStatus) return;
        var radio = row.querySelector('input[type=radio][value="' + status + '"]');
        if (radio) radio.checked = true;
      });
      updateCount();
    });
  });

  var clearBtn = document.getElementById("clearAll");
  if (clearBtn) {
    clearBtn.addEventListener("click", function () {
      form.querySelectorAll("input[type=radio]").forEach(function (radio) {
        radio.checked = false;
      });
      updateCount();
    });
  }

  if (notifyToggle) {
    notifyToggle.addEventListener("change", syncSaveLabel);
    syncSaveLabel();
  }

  function collectRecords() {
    return rows.reduce(function (acc, row) {
      var status = statusOf(row);
      if (!status) return acc;
      var note = row.querySelector(".note-input");
      acc.push({
        employee_id: parseInt(row.dataset.employee, 10),
        status: status,
        note: note ? note.value : "",
        check_in_time: row.querySelector(".check-in").value,
        check_out_time: row.querySelector(".check-out").value,
        collections: linesOf(row)
      });
      return acc;
    }, []);
  }

  function showResult(data) {
    resultBox.hidden = false;
    resultBox.classList.toggle("has-error", Boolean(data.failed) || data.ok === false);

    if (data.ok === false) {
      resultBox.innerHTML = "<strong>" + escapeHtml(data.error || "Something went wrong.") + "</strong>";
      return;
    }

    var html = "<strong>" + escapeHtml(data.message) + "</strong>";
    if (data.failed) {
      html += "<div>" + data.failed + " mail(s) could not be delivered:</div><ul>";
      data.failures.forEach(function (failure) {
        html += "<li>" + escapeHtml(failure.name) + " — " + escapeHtml(failure.error) + "</li>";
      });
      html += "</ul>";
    }
    resultBox.innerHTML = html;
  }

  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();

    var records = collectRecords();
    if (!records.length) return;

    // Week offs and holidays are saved but never mailed, so keep them out of the count.
    var mailable = records.filter(function (record) {
      return record.status !== "WEEK_OFF" && record.status !== "HOLIDAY";
    }).length;

    var notify = notifyToggle ? notifyToggle.checked : true;
    if (notify && !window.confirm("Save attendance and email " + mailable + " employee(s)?")) {
      return;
    }

    var originalLabel = saveBtn.textContent;
    saveBtn.disabled = true;
    saveBtn.textContent = notify ? "Saving & sending…" : "Saving…";

    fetch(form.dataset.url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(),
        "X-Requested-With": "XMLHttpRequest"
      },
      body: JSON.stringify({
        date: form.dataset.date,
        notify: notify,
        records: records
      })
    })
      .then(function (response) {
        return response.json().catch(function () {
          throw new Error("Server returned an unexpected response (" + response.status + ").");
        });
      })
      .then(function (data) {
        showResult(data);
        if (data.ok) {
          rows.forEach(function (row) {
            if (row.querySelector("input[type=radio]:checked")) row.classList.add("saved");
          });
        }
      })
      .catch(function (error) {
        showResult({ ok: false, error: error.message });
      })
      .finally(function () {
        saveBtn.disabled = false;
        saveBtn.textContent = originalLabel;
        updateCount();
        resultBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
  });

  updateCount();
})();
