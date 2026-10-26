(function () {
  "use strict";
  var LABEL = { operational: "Operational", degraded: "Degraded", outage: "Down", unknown: "Unknown" };
  var REFRESH_MS = 60000;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function when(iso) {
    var date = new Date(iso);
    return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  function duration(from, to) {
    var minutes = Math.max(1, Math.round((new Date(to) - new Date(from)) / 60000));
    if (minutes < 60) return minutes + " min";
    var hours = Math.floor(minutes / 60);
    return hours + " h " + (minutes % 60) + " min";
  }

  function renderComponent(c) {
    var box = el("div", "component");
    var row = el("div", "row");
    row.appendChild(el("span", "name", c.name));
    row.appendChild(el("span", "state " + c.state, LABEL[c.state] || c.state));
    box.appendChild(row);
    if (c.description) box.appendChild(el("p", "muted", c.description));
    if (c.detail) box.appendChild(el("p", "detail", c.detail));
    var bars = el("div", "bars");
    bars.style.setProperty("--n", c.days.length);
    var label = c.uptime === null ? "No history yet" : c.uptime.toFixed(2) + "% uptime over " + c.days.length + " days";
    bars.setAttribute("role", "img");
    bars.setAttribute("aria-label", c.name + ": " + label);
    c.days.forEach(function (d) {
      var bar = el("div", "bar " + d.s);
      bar.title = d.d + ": " + (d.s === "unknown" ? "no data" : (LABEL[d.s] + ", " + d.u.toFixed(2) + "% up"));
      bars.appendChild(bar);
    });
    box.appendChild(bars);
    var scale = el("div", "scale");
    scale.appendChild(el("span", "", c.days.length + " days ago"));
    scale.appendChild(el("span", "", label));
    scale.appendChild(el("span", "", "Today"));
    box.appendChild(scale);
    return box;
  }

  function renderIncident(e) {
    var item = el("div", "incident-item");
    item.appendChild(el("p", "name", e.name + (e.state === "outage" ? ": down" : ": degraded")));
    var span = e.end ? when(e.start) + ", lasted " + duration(e.start, e.end) : "Since " + when(e.start) + " (ongoing)";
    item.appendChild(el("p", "muted", span));
    if (e.detail) item.appendChild(el("p", "detail", e.detail));
    return item;
  }

  function render(status) {
    var overall = document.getElementById("overall");
    overall.className = "overall " + status.overall.state;
    document.getElementById("overall-message").textContent = status.overall.message;
    document.getElementById("updated").textContent = "Updated " + when(status.generated_at);

    var ageMinutes = (Date.now() - new Date(status.generated_at)) / 60000;
    var stale = document.getElementById("stale");
    stale.hidden = ageMinutes <= status.stale_after_minutes;
    stale.textContent = "These results are " + Math.round(ageMinutes) + " minutes old: the checks have not run recently, so the state below may be out of date.";

    var notices = document.getElementById("notices");
    notices.replaceChildren();
    status.notices.forEach(function (n) {
      var box = el("div", "banner " + (n.level === "incident" ? "incident" : ""));
      box.appendChild(el("strong", "", n.title));
      if (n.body) box.appendChild(el("span", "", n.body));
      notices.appendChild(box);
    });

    var groups = document.getElementById("groups");
    groups.replaceChildren();
    status.groups.forEach(function (g) {
      var section = el("section", "group");
      section.appendChild(el("h2", "", g.name));
      g.components.forEach(function (c) { section.appendChild(renderComponent(c)); });
      groups.appendChild(section);
    });

    var incidents = document.getElementById("incidents");
    incidents.replaceChildren();
    var list = status.incidents.open.concat(status.incidents.recent);
    if (!list.length) incidents.appendChild(el("p", "muted", "No incidents in the last 14 days."));
    list.forEach(function (e) { incidents.appendChild(renderIncident(e)); });
  }

  function load() {
    fetch("status.json?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(render)
      .catch(function () {
        document.getElementById("overall-message").textContent = "Status is temporarily unavailable";
        document.getElementById("overall").className = "overall unknown";
      });
  }

  load();
  setInterval(load, REFRESH_MS);
})();
