"""DOM glue for the SLO calculator. Logic lives in slo.py and charts.py."""

from __future__ import annotations

import math

from pyscript import document, window
from pyodide.ffi import create_proxy

from charts import LIGHT, TIMELINE_LABEL_W, burndown_svg, burn_rate_svg, cursor_fraction, timeline_svg
from slo import (
    INCIDENT_KINDS,
    allowance_summary,
    allowed_bad_requests,
    allowed_downtime,
    budget_status,
    compare,
    default_incidents,
    flag_spans,
    format_burn,
    format_count,
    format_duration,
    format_percent,
    incident_spans,
    nines_table,
    parse_share,
    period_seconds,
    sample_at,
    share_query,
    simulate,
    status_summary,
)

PRESET_IDS = {
    99.0: "slo-99",
    99.5: "slo-99-5",
    99.9: "slo-99-9",
    99.95: "slo-99-95",
    99.99: "slo-99-99",
}
UNIT_SECONDS = {"minutes": 60.0, "hours": 3600.0, "days": 86400.0}

_proxies = []
_silent = False
_touched = {"bad": False}
_cache = {"key": None, "sim": None}
_drawn = {"key": None, "colors": None}
_playing = False
_timer = None


def el(tag, text=None, attrs=None):
    node = document.createElement(tag)
    if text is not None:
        node.textContent = str(text)
    if attrs:
        for key, value in attrs.items():
            node.setAttribute(key, "" if value is None else str(value))
    return node


def clear(node):
    node.innerHTML = ""


def by_id(element_id):
    return document.getElementById(element_id)


def number(element_id, default=0.0):
    raw = str(by_id(element_id).value).strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    if math.isnan(value) or math.isinf(value):
        return default
    return value


def palette():
    style = window.getComputedStyle(document.documentElement)
    names = (
        "ink", "muted", "line", "accent", "enamel", "surface",
        "fast", "slow", "ticket", "naive",
    )
    colors = {}
    for name in names:
        value = str(style.getPropertyValue("--" + name)).strip()
        colors[name] = value or LIGHT[name]
    grid = str(style.getPropertyValue("--chart-grid")).strip()
    colors["grid"] = grid or colors["line"]
    return colors


def _scrub_fraction():
    scrub = by_id("scrub")
    maximum = float(str(scrub.max) or "1")
    if maximum <= 0:
        return 1.0
    return min(1.0, max(0.0, float(str(scrub.value)) / maximum))


def _sync_scrub(window_days):
    """Keep one scrubber step equal to one minute when the window changes."""
    global _silent
    scrub = by_id("scrub")
    minutes = int(window_days) * 24 * 60
    current = float(str(scrub.max) or "1")
    if int(current) == minutes:
        return
    fraction = float(str(scrub.value)) / current if current else 1.0
    _silent = True
    scrub.max = str(minutes)
    scrub.value = str(int(round(min(1.0, max(0.0, fraction)) * minutes)))
    _silent = False


def read_state():
    slo_node = document.querySelector('input[name="slo"]:checked')
    slo_choice = str(slo_node.value) if slo_node else "99.9"
    if slo_choice == "custom":
        slo = min(100.0, max(0.0, number("slo-custom", 99.9)))
    else:
        slo = float(slo_choice)
    sli_node = document.querySelector('input[name="sli"]:checked')
    sli = str(sli_node.value) if sli_node else "requests"
    unit = str(by_id("unit").value)
    if unit not in UNIT_SECONDS:
        unit = "days"
    incidents = [kind for kind in INCIDENT_KINDS if bool(by_id("inc-" + kind).checked)]
    window_days = int(number("window", 30))
    if window_days not in (7, 28, 30, 90):
        window_days = 30
    _sync_scrub(window_days)
    return {
        "sli": sli,
        "slo": slo,
        "window": window_days,
        "rps": min(1e7, max(0.0, number("rps", 0))),
        "bad": max(0.0, number("bad", 0)),
        "elapsed": max(0.0, number("elapsed", 0)) * UNIT_SECONDS[unit],
        "unit": unit,
        "incidents": incidents,
        "t": _scrub_fraction(),
    }


def _input_number(value):
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def apply_share(parsed):
    if "sli" in parsed:
        by_id("sli-" + parsed["sli"]).checked = True
    if "slo" in parsed:
        matched = False
        for preset, element_id in PRESET_IDS.items():
            if abs(parsed["slo"] - preset) < 1e-9:
                by_id(element_id).checked = True
                matched = True
                break
        if not matched:
            by_id("slo-custom-radio").checked = True
            by_id("slo-custom").value = _input_number(parsed["slo"])
    if "window" in parsed:
        by_id("window").value = str(parsed["window"])
    if "rps" in parsed:
        by_id("rps").value = _input_number(parsed["rps"])
    if "unit" in parsed:
        by_id("unit").value = parsed["unit"]
    if "elapsed" in parsed:
        unit = parsed.get("unit", str(by_id("unit").value))
        factor = UNIT_SECONDS.get(unit, 86400.0)
        by_id("elapsed").value = _input_number(parsed["elapsed"] / factor)
    if "bad" in parsed:
        by_id("bad").value = _input_number(parsed["bad"])
        _touched["bad"] = True
    if "incidents" in parsed:
        chosen = set(parsed["incidents"])
        for kind in INCIDENT_KINDS:
            by_id("inc-" + kind).checked = kind in chosen
    if "t" in parsed:
        days = int(parsed.get("window") or number("window", 30))
        minutes = days * 24 * 60
        by_id("scrub").max = str(minutes)
        by_id("scrub").value = str(int(round(parsed["t"] * minutes)))


def write_url(state):
    path = str(window.location.pathname)
    window.history.replaceState(None, "", path + "?" + share_query(state))


def on_pace_bad(state):
    if state["sli"] == "requests":
        allowed = allowed_bad_requests(state["slo"], state["window"], state["rps"])
    else:
        allowed = allowed_downtime(state["slo"], state["window"])
    window_seconds = period_seconds(state["window"])
    if window_seconds <= 0:
        return 0.0
    return allowed * (state["elapsed"] / window_seconds)


def get_sim(state):
    key = (round(state["slo"], 6), state["window"], tuple(state["incidents"]))
    if _cache["key"] != key:
        incidents = [item for item in default_incidents(state["window"]) if item.kind in state["incidents"]]
        _cache["sim"] = simulate(state["slo"], state["window"], incidents)
        _cache["key"] = key
    return _cache["sim"]


def render_allowance(state):
    host = by_id("allowance-body")
    clear(host)
    slo = state["slo"]
    days = state["window"]
    rps = state["rps"]
    stats = (
        (format_duration(allowed_downtime(slo, days)), "in this window"),
        (format_duration(allowed_downtime(slo, 1)), "per day"),
        (format_duration(allowed_downtime(slo, 7)), "per week"),
        (format_duration(allowed_downtime(slo, 30)), "per 30-day month"),
        (format_count(allowed_bad_requests(slo, days, rps)), "bad requests in the window"),
    )
    listing = el("ul", attrs={"class": "stats"})
    for value, caption in stats:
        item = el("li")
        item.appendChild(el("p", value, {"class": "stat"}))
        item.appendChild(el("p", caption))
        listing.appendChild(item)
    host.appendChild(listing)
    host.appendChild(el("p", allowance_summary(slo, days, rps), {"class": "summary"}))

    table = el("table")
    table.appendChild(el("caption", "Nines, and the downtime each one allows"))
    head = el("tr")
    for heading in ("SLO", "Per day", "Per week", "Per 30 days", "This window", "Bad requests"):
        head.appendChild(el("th", heading, {"scope": "col"}))
    thead = el("thead")
    thead.appendChild(head)
    table.appendChild(thead)
    body = el("tbody")
    for row in nines_table(days, rps, highlight=slo):
        tr = el("tr")
        if row.current:
            tr.setAttribute("class", "is-current")
            tr.setAttribute("aria-current", "true")
        cells = (
            row.label,
            format_duration(row.downtime_day),
            format_duration(row.downtime_week),
            format_duration(row.downtime_month),
            format_duration(row.downtime_window),
            format_count(row.bad_requests),
        )
        for index, value in enumerate(cells):
            cell = el("th" if index == 0 else "td", value)
            if index == 0:
                cell.setAttribute("scope", "row")
            tr.appendChild(cell)
        body.appendChild(tr)
    table.appendChild(body)
    wrap = el("div", attrs={"class": "table-wrap"})
    wrap.appendChild(table)
    host.appendChild(wrap)


def render_status(state):
    by_id("bad-caption").textContent = (
        "Downtime so far, seconds" if state["sli"] == "time" else "Bad requests so far"
    )
    status = budget_status(
        state["slo"], state["window"], state["bad"], state["elapsed"], state["rps"], state["sli"]
    )
    host = by_id("status-readout")
    clear(host)
    consumed = "∞" if math.isinf(status.consumed) else format_percent(status.consumed)
    if math.isinf(status.remaining):
        remaining = "−∞"
    else:
        remaining = format_percent(status.remaining)
    until = "—" if status.seconds_to_exhaustion is None else format_duration(status.seconds_to_exhaustion)
    listing = el("ul", attrs={"class": "stats"})
    for value, caption in (
        (consumed, "of the budget consumed"),
        (remaining, "of the budget remaining"),
        (format_burn(status.burn_rate), "current burn rate"),
        (until, "until the budget is gone, at this rate"),
    ):
        item = el("li")
        item.appendChild(el("p", value, {"class": "stat"}))
        item.appendChild(el("p", caption))
        listing.appendChild(item)
    host.appendChild(listing)
    host.appendChild(el("p", status_summary(status), {"class": "summary"}))
    return status_summary(status)


def _percent_or_dash(value):
    if value is None:
        return "—"
    return f"{value * 100:.0f}%"


def render_specs(sim):
    host = by_id("policy")
    clear(host)
    for spec in sim.specs:
        percent = spec.budget_slice(sim.window_seconds) * 100.0
        verb = "Pages" if spec.severity == "page" else "Opens a ticket"
        host.appendChild(el(
            "li",
            f"{verb} at {format_burn(spec.threshold)} when both the last "
            f"{format_duration(spec.long_seconds)} and the last "
            f"{format_duration(spec.short_seconds)} are over that rate. "
            f"That long window is about {percent:.0f}% of this budget.",
        ))


def render_compare(sim):
    host = by_id("compare")
    clear(host)
    scores = compare(sim)
    table = el("table")
    table.appendChild(el("caption", "Multi-window alerts against the long window alone"))
    head = el("tr")
    for heading in ("Policy", "Precision", "Recall", "Detection", "Reset", "Times fired"):
        head.appendChild(el("th", heading, {"scope": "col"}))
    thead = el("thead")
    thead.appendChild(head)
    table.appendChild(thead)
    body = el("tbody")
    for score in scores:
        tr = el("tr")
        tr.appendChild(el("th", score.label, {"scope": "row"}))
        tr.appendChild(el("td", _percent_or_dash(score.precision)))
        tr.appendChild(el("td", _percent_or_dash(score.recall)))
        tr.appendChild(el("td", format_duration(score.detection_seconds)))
        tr.appendChild(el("td", format_duration(score.reset_seconds)))
        tr.appendChild(el("td", str(score.alert_spans)))
        body.appendChild(tr)
    table.appendChild(body)
    host.appendChild(table)
    host.appendChild(el(
        "p",
        "Precision is the share of alert minutes that fell during an incident. "
        "Recall is the share of incident minutes the alert covered. "
        "Detection is the wait from the start of an incident to the alert. "
        "Reset is how long the alert stayed open after the incident ended. "
        "A dash means there was nothing to measure.",
        {"class": "summary"},
    ))


def render_transitions(sim):
    host = by_id("transitions")
    clear(host)
    rows = []
    for spec in sim.specs:
        for start, end in flag_spans(sim.multi[spec.id], sim.step):
            rows.append((
                start,
                f"{spec.name} fired at {format_duration(start)} and reset at {format_duration(end)}.",
            ))
    rows.sort()
    if not rows:
        host.appendChild(el("p", "No multi-window alert fires on this stream."))
        return
    listing = el("ul")
    for _, text in rows[:8]:
        listing.appendChild(el("li", text))
    host.appendChild(listing)
    if len(rows) > 8:
        host.appendChild(el("p", f"And {len(rows) - 8} more. The chart and the table include all of them.", {"class": "note"}))


def render_charts(sim, colors):
    by_id("burndown").innerHTML = burndown_svg(
        sim.remaining,
        sim.window_seconds,
        colors,
        title="Error budget remaining over the window",
    )
    by_id("burn").innerHTML = burn_rate_svg(
        {spec.id: sim.burn_long[spec.id] for spec in sim.specs},
        {spec.id: spec.threshold for spec in sim.specs},
        sim.window_seconds,
        colors,
        title="Long-window burn rate on a logarithmic scale",
    )
    lanes = [{
        "label": "Errors",
        "spans": incident_spans(sim),
        "color": colors["accent"],
        "hatched": False,
    }]
    for spec in sim.specs:
        short_name = spec.name.split()[0]
        lanes.append({
            "label": spec.name,
            "spans": flag_spans(sim.multi[spec.id], sim.step),
            "color": colors[spec.id],
            "hatched": False,
        })
        lanes.append({
            "label": short_name + ", long only",
            "spans": flag_spans(sim.naive[spec.id], sim.step),
            "color": colors["naive"],
            "hatched": True,
        })
    by_id("timeline").innerHTML = timeline_svg(
        sim.window_seconds,
        lanes,
        colors,
        title="When each alert fires and resets",
    )
    end = sim.remaining[-1] if sim.remaining else None
    end_text = "no samples" if end is None or math.isinf(end) else format_percent(end) + " remaining"
    by_id("burndown-text").textContent = "Error budget burndown. The window ends at " + end_text + "."
    by_id("burn-text").textContent = (
        "Burn rate of the long window for the fast page, the slow page, and the ticket."
    )
    by_id("timeline-text").textContent = (
        "Timeline of injected errors, multi-window alerts, and long-window-only alerts."
    )
    render_specs(sim)
    render_compare(sim)
    render_transitions(sim)


def render_cursor(state, sim):
    moment = state["t"] * sim.window_seconds
    sample = sample_at(sim, moment)
    by_id("scrub-readout").textContent = (
        f"{format_duration(sample['t'])} into the window. "
        f"Budget remaining {format_percent(sample['remaining'])}."
    )
    firing = []
    for spec in sim.specs:
        info = sample["alerts"][spec.id]
        if info["multi"]:
            firing.append(f"{spec.name} multi-window alert is firing")
        elif info["naive"]:
            firing.append(f"{spec.name} long-window alert is still firing")
    text = "No alert is firing at this moment." if not firing else ". ".join(firing) + "."
    live = by_id("firing-now")
    if str(live.textContent) != text:
        live.textContent = text
    chart = cursor_fraction(sample["t"], 0, sim.window_seconds)
    lane = cursor_fraction(sample["t"], 0, sim.window_seconds, left=TIMELINE_LABEL_W, right=10)
    for element_id, fraction in (
        ("burndown-cursor", chart),
        ("burn-cursor", chart),
        ("timeline-cursor", lane),
    ):
        node = by_id(element_id)
        node.hidden = False
        node.style.left = f"{fraction * 100:.3f}%"


def render():
    global _silent
    state = read_state()
    if not _touched["bad"]:
        paced = on_pace_bad(state)
        shown = round(paced) if state["sli"] == "requests" else round(paced, 1)
        _silent = True
        by_id("bad").value = _input_number(shown)
        _silent = False
        state = read_state()
    sim = get_sim(state)
    render_allowance(state)
    summary = render_status(state)
    colors = palette()
    color_key = tuple(colors.get(name) for name in ("ink", "enamel", "fast", "grid"))
    if _drawn["key"] != _cache["key"] or _drawn["colors"] != color_key:
        render_charts(sim, colors)
        _drawn["key"] = _cache["key"]
        _drawn["colors"] = color_key
    render_cursor(state, sim)
    by_id("live-summary").textContent = allowance_summary(state["slo"], state["window"], state["rps"]) + " " + summary
    write_url(state)


def on_input(event):
    global _silent
    if _silent:
        return
    target = event.target
    target_id = str(target.id) if target else ""
    if target_id in ("scrub", "speed"):
        if target_id == "scrub":
            state = read_state()
            render_cursor(state, get_sim(state))
            write_url(state)
        return
    if target_id == "bad":
        _touched["bad"] = True
    if target_id == "slo-custom":
        by_id("slo-custom-radio").checked = True
    render()


def stop_play():
    global _playing, _timer
    _playing = False
    if _timer is not None:
        window.clearInterval(_timer)
        _timer = None
    button = by_id("play")
    button.textContent = "Play"
    button.setAttribute("aria-pressed", "false")


def tick():
    scrub = by_id("scrub")
    maximum = float(str(scrub.max) or "1")
    fraction = float(str(scrub.value)) / maximum
    speed = number("speed", 0.05)
    fraction += speed * 0.1
    if fraction >= 1:
        fraction = 1
        scrub.value = str(int(maximum))
        stop_play()
    else:
        scrub.value = str(int(round(fraction * maximum)))
    state = read_state()
    render_cursor(state, get_sim(state))
    write_url(state)


def toggle_play(_event):
    global _playing, _timer
    if _playing:
        stop_play()
        return
    scrub = by_id("scrub")
    maximum = float(str(scrub.max) or "1")
    if float(str(scrub.value)) >= maximum - 1:
        scrub.value = "0"
    _playing = True
    button = by_id("play")
    button.textContent = "Pause"
    button.setAttribute("aria-pressed", "true")
    _timer = window.setInterval(_proxies[0], 100)


def on_submit(event):
    event.preventDefault()
    render()


def on_scheme(_event):
    _drawn["colors"] = None
    render()


def boot():
    parsed = parse_share(str(window.location.search))
    apply_share(parsed)
    _proxies.append(create_proxy(tick))
    _proxies.append(create_proxy(on_input))
    _proxies.append(create_proxy(toggle_play))
    _proxies.append(create_proxy(on_submit))
    _proxies.append(create_proxy(on_scheme))
    by_id("calculator").addEventListener("input", _proxies[1])
    by_id("play").addEventListener("click", _proxies[2])
    by_id("calculator").addEventListener("submit", _proxies[3])
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", _proxies[4])
    by_id("play").disabled = False
    render()


try:
    boot()
    document.body.classList.add("py-ready")
    by_id("loading").hidden = True
except Exception as exc:
    error = by_id("loading-error")
    error.hidden = False
    error.textContent = "Python started, then hit an error: " + str(exc)
    raise
