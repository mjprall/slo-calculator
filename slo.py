"""Error-budget math and multi-window burn-rate alerts.

Pure functions. No DOM and no PyScript. Times are seconds unless a name says
otherwise. A burn rate of 1 spends the error budget exactly at the end of the
SLO window.

Alert rules follow the Google SRE workbook, scaled to the chosen window:

- page at 14.4× over 1 hour, confirmed over 5 minutes (about 2% of a 30-day budget)
- page at 6× over 6 hours, confirmed over 30 minutes (about 5%)
- ticket at 1× over 3 days, confirmed over 6 hours (about 10%)

Lookback durations scale with ``window_days / 30``. The multipliers stay put,
so each alert still means the same fraction of whatever window you picked.
The 30-day case matches the workbook exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode

PRESETS: tuple[float, ...] = (99.0, 99.5, 99.9, 99.95, 99.99)
WINDOWS: tuple[int, ...] = (7, 28, 30, 90)
STEP = 60
NINE_LEVELS: tuple[float, ...] = (90.0, 99.0, 99.5, 99.9, 99.95, 99.99, 99.999)
INCIDENT_KINDS: tuple[str, ...] = ("outage", "degradation", "flap")

# Workbook lookbacks for a 30-day SLO, in seconds: long, short.
_WORKBOOK = (
    ("fast", "Fast burn", "page", 14.4, 3600.0, 300.0),
    ("slow", "Slow burn", "page", 6.0, 21600.0, 1800.0),
    ("ticket", "Ticket", "ticket", 1.0, 259200.0, 21600.0),
)


def budget_fraction(slo_percent: float) -> float:
    """Share of events that may fail. 99.9 → 0.001."""
    slo = float(slo_percent)
    if slo < 0.0 or slo > 100.0 or math.isnan(slo):
        raise ValueError("SLO percent must be between 0 and 100")
    return (100.0 - slo) / 100.0


def period_seconds(days: float) -> float:
    days = float(days)
    if days <= 0.0 or math.isnan(days):
        raise ValueError("period must be positive")
    return days * 86400.0


def allowed_downtime(slo_percent: float, days: float) -> float:
    """Downtime, in seconds, that spends the whole budget over ``days``."""
    return budget_fraction(slo_percent) * period_seconds(days)


def allowed_bad_requests(slo_percent: float, days: float, rps: float) -> float:
    """Failed requests that spend the whole budget over ``days`` at ``rps``."""
    rps = float(rps)
    if rps < 0.0 or math.isnan(rps):
        raise ValueError("requests per second must be non-negative")
    return budget_fraction(slo_percent) * rps * period_seconds(days)


def format_slo(slo_percent: float) -> str:
    value = float(slo_percent)
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value))}%"
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{text}%"


def format_duration(seconds: float | None) -> str:
    """Short duration for a person reading a table, not a log line."""
    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)):
        return "—"
    if math.isinf(seconds):
        return "∞" if seconds > 0 else "0 s"
    sign = "−" if seconds < 0 else ""
    remaining = abs(float(seconds))
    if remaining < 0.05:
        return f"{sign}0 s"
    if remaining >= 86400 and abs(remaining / 86400 - round(remaining / 86400)) < 1e-6:
        days = int(round(remaining / 86400))
        unit = "day" if days == 1 else "days"
        return f"{sign}{days} {unit}"
    if remaining >= 3600 and abs(remaining / 3600 - round(remaining / 3600)) < 1e-6 and remaining < 48 * 3600:
        hours = int(round(remaining / 3600))
        unit = "hour" if hours == 1 else "hours"
        return f"{sign}{hours} {unit}"
    if 60 <= remaining < 90 * 60 and abs(remaining / 60 - round(remaining / 60)) < 1e-6:
        return f"{sign}{int(round(remaining / 60))} min"
    if remaining < 120:
        if abs(remaining - round(remaining)) < 0.05:
            return f"{sign}{int(round(remaining))} s"
        return f"{sign}{remaining:.1f} s"
    if remaining < 90 * 60:
        return f"{sign}{remaining / 60:.1f} min"
    if remaining < 48 * 3600:
        return f"{sign}{remaining / 3600:.1f} hours"
    return f"{sign}{remaining / 86400:.1f} days"


def format_count(number: float | None) -> str:
    if number is None or (isinstance(number, float) and math.isnan(number)):
        return "—"
    if math.isinf(number):
        return "∞"
    sign = "−" if number < 0 else ""
    magnitude = abs(float(number))
    if magnitude >= 1e12:
        return f"{sign}{magnitude / 1e12:.2f} trillion"
    if magnitude >= 1e9:
        return f"{sign}{magnitude / 1e9:.2f} billion"
    if magnitude >= 1e6:
        return f"{sign}{magnitude / 1e6:.2f} million"
    if abs(magnitude - round(magnitude)) < 1e-6:
        return f"{sign}{int(round(magnitude)):,}"
    if magnitude >= 100:
        return f"{sign}{magnitude:,.1f}"
    return f"{sign}{magnitude:,.2f}"


def format_percent(fraction: float | None) -> str:
    if fraction is None or (isinstance(fraction, float) and math.isnan(fraction)):
        return "—"
    if math.isinf(fraction):
        return "∞"
    percent = float(fraction) * 100.0
    absolute = abs(percent)
    if absolute >= 100:
        return f"{percent:.0f}%"
    if absolute >= 10:
        return f"{percent:.1f}%"
    return f"{percent:.2f}%"


def format_burn(rate: float | None) -> str:
    if rate is None or (isinstance(rate, float) and math.isnan(rate)):
        return "—"
    if math.isinf(rate):
        return "∞"
    absolute = abs(rate)
    if absolute >= 100:
        text = f"{rate:.0f}"
    elif absolute >= 10:
        text = f"{rate:.1f}"
    else:
        text = f"{rate:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}×"


@dataclass(frozen=True)
class BudgetStatus:
    allowed: float
    bad: float
    consumed: float
    remaining: float
    burn_rate: float | None
    seconds_to_exhaustion: float | None
    elapsed_seconds: float
    window_seconds: float
    unit: str


def budget_status(
    slo_percent: float,
    window_days: float,
    bad: float,
    elapsed_seconds: float,
    rps: float,
    sli: str,
) -> BudgetStatus:
    """Where the budget stands partway through the window.

    ``sli`` is ``"requests"`` (``bad`` is a count) or ``"time"`` (``bad`` is
    downtime in seconds). Burn rate is budget-fraction consumed divided by
    window-fraction elapsed. It does not depend on which unit you picked,
    only on how much of the allowance is gone.
    """
    if sli not in ("requests", "time"):
        raise ValueError("sli must be 'requests' or 'time'")
    bad = float(bad)
    elapsed = float(elapsed_seconds)
    if bad < 0.0 or math.isnan(bad):
        raise ValueError("bad events must be non-negative")
    if elapsed < 0.0 or math.isnan(elapsed):
        raise ValueError("elapsed time must be non-negative")
    window = period_seconds(window_days)
    if sli == "requests":
        allowed = allowed_bad_requests(slo_percent, window_days, rps)
    else:
        allowed = allowed_downtime(slo_percent, window_days)

    if allowed == 0.0:
        consumed = 0.0 if bad == 0.0 else math.inf
    else:
        consumed = bad / allowed
    remaining = 0.0 if math.isinf(consumed) else 1.0 - consumed

    burn: float | None
    exhaust: float | None
    if elapsed <= 0.0:
        burn = None
        exhaust = None
    elif allowed == 0.0:
        burn = 0.0 if bad == 0.0 else math.inf
        exhaust = None if bad == 0.0 else 0.0
    elif bad == 0.0:
        burn = 0.0
        exhaust = None
    else:
        burn = consumed * (window / elapsed)
        if consumed >= 1.0:
            exhaust = 0.0
        else:
            exhaust = (allowed - bad) / (bad / elapsed)

    return BudgetStatus(
        allowed=allowed,
        bad=bad,
        consumed=consumed,
        remaining=remaining,
        burn_rate=burn,
        seconds_to_exhaustion=exhaust,
        elapsed_seconds=elapsed,
        window_seconds=window,
        unit="requests" if sli == "requests" else "seconds",
    )


def status_summary(status: BudgetStatus) -> str:
    if status.elapsed_seconds <= 0.0:
        return "Enter how long this window has been open to calculate a burn rate."
    if status.allowed == 0.0 and status.bad <= 0.0:
        return "This SLO allows no bad events, and none have been recorded."
    if status.allowed == 0.0 and status.bad > 0.0:
        return "This SLO allows no bad events. The budget is already exhausted."
    if status.consumed >= 1.0:
        return f"The error budget is already exhausted ({format_percent(status.consumed)} consumed)."
    if status.burn_rate == 0.0:
        return "No budget has been spent. The burn rate is 0×."
    if status.burn_rate is None or status.seconds_to_exhaustion is None:
        return "Enter how long this window has been open to calculate a burn rate."
    return (
        f"Burn rate is {format_burn(status.burn_rate)}. At this pace the remaining "
        f"budget lasts {format_duration(status.seconds_to_exhaustion)}."
    )


def allowance_summary(slo_percent: float, window_days: float, rps: float) -> str:
    downtime = allowed_downtime(slo_percent, window_days)
    bad = allowed_bad_requests(slo_percent, window_days, rps)
    sentence = (
        f"A {format_slo(slo_percent)} SLO over {format_duration(period_seconds(window_days))} "
        f"allows {format_duration(downtime)} of downtime"
    )
    if rps > 0:
        return sentence + f", about {format_count(bad)} bad requests at {format_count(rps)} per second."
    return sentence + "."


@dataclass(frozen=True)
class NinesRow:
    slo: float
    label: str
    downtime_day: float
    downtime_week: float
    downtime_month: float
    downtime_window: float
    bad_requests: float
    current: bool


def nines_table(window_days: float, rps: float, highlight: float | None = None) -> list[NinesRow]:
    """Classic nines, plus the selected SLO if it is not already in the list."""
    levels = list(NINE_LEVELS)
    if highlight is not None and not any(abs(highlight - level) < 1e-9 for level in levels):
        levels.append(float(highlight))
        levels.sort()
    rows = []
    for level in levels:
        rows.append(
            NinesRow(
                slo=level,
                label=format_slo(level),
                downtime_day=allowed_downtime(level, 1),
                downtime_week=allowed_downtime(level, 7),
                downtime_month=allowed_downtime(level, 30),
                downtime_window=allowed_downtime(level, window_days),
                bad_requests=allowed_bad_requests(level, window_days, rps),
                current=highlight is not None and abs(level - highlight) < 1e-9,
            )
        )
    return rows


@dataclass(frozen=True)
class AlertSpec:
    id: str
    name: str
    severity: str
    threshold: float
    long_seconds: float
    short_seconds: float

    def budget_slice(self, window_seconds: float) -> float:
        """Fraction of the SLO budget the long window spends at the threshold."""
        if window_seconds <= 0:
            return 0.0
        return self.threshold * self.long_seconds / window_seconds


def alert_specs(window_days: float) -> tuple[AlertSpec, ...]:
    """Workbook alerts, with lookbacks scaled by ``window_days / 30``."""
    scale = float(window_days) / 30.0
    if scale <= 0.0 or math.isnan(scale):
        raise ValueError("window must be positive")
    window = period_seconds(window_days)
    specs = []
    for ident, name, severity, threshold, long_s, short_s in _WORKBOOK:
        long_scaled = min(window, long_s * scale)
        short_scaled = short_s * scale
        if short_scaled >= long_scaled:
            short_scaled = long_scaled / 2.0
        specs.append(
            AlertSpec(
                id=ident,
                name=name,
                severity=severity,
                threshold=threshold,
                long_seconds=long_scaled,
                short_seconds=short_scaled,
            )
        )
    return tuple(specs)


def _window_buckets(seconds: float) -> int:
    return max(1, int(round(seconds / STEP)))


@dataclass(frozen=True)
class Incident:
    kind: str
    start: float
    duration: float
    peak: float = 1.0
    on: float = 180.0
    off: float = 720.0


def default_incidents(window_days: float) -> tuple[Incident, ...]:
    """Three scripted incidents: outage, slow burn, and flap.

    Durations stay fixed (25 minutes, 12 hours, 6 hours) so a short window
    still shows a real outage and a long window does not turn it into a
    multi-day event. Starts sit at 15%, 40%, and 70% of the window.
    """
    window = period_seconds(window_days)
    planned = (
        ("outage", 0.15, 25 * 60, 1.0, 180.0, 720.0),
        ("degradation", 0.40, 12 * 3600, 8.0, 180.0, 720.0),
        ("flap", 0.70, 6 * 3600, 1.0, 180.0, 720.0),
    )
    incidents = []
    for kind, frac, duration, peak, on, off in planned:
        start = frac * window
        length = duration
        if start + length > window:
            start = max(0.0, window - length)
        if start + length > window:
            length = window - start
        if length > 0:
            incidents.append(Incident(kind, start, length, peak, on, off))
    return tuple(incidents)


def error_ratio_at(t: float, incidents: tuple[Incident, ...] | list[Incident], fraction: float) -> float:
    """Failed share of the minute starting at ``t``. Overlaps take the max.

    Degradation ``peak`` is a multiple of the budget error rate (8 means 8×),
    capped at a total outage. Flapping is ``on`` seconds of ``peak`` followed
    by ``off`` seconds healthy, repeating through the incident.
    """
    ratio = 0.0
    for incident in incidents:
        if t < incident.start or t >= incident.start + incident.duration:
            continue
        if incident.kind == "outage":
            local = min(1.0, max(0.0, incident.peak))
        elif incident.kind == "degradation":
            local = min(1.0, max(0.0, incident.peak * fraction))
        elif incident.kind == "flap":
            cycle = incident.on + incident.off
            if cycle <= 0:
                continue
            phase = (t - incident.start) % cycle
            local = min(1.0, max(0.0, incident.peak)) if phase < incident.on else 0.0
        else:
            raise ValueError(f"unknown incident kind {incident.kind!r}")
        if local > ratio:
            ratio = local
    return ratio


def _burn(mean_ratio: float, fraction: float) -> float:
    if fraction == 0.0:
        return math.inf if mean_ratio > 0.0 else 0.0
    return mean_ratio / fraction


@dataclass
class Simulation:
    step: int
    window_seconds: float
    budget_fraction: float
    specs: tuple[AlertSpec, ...]
    incidents: tuple[Incident, ...]
    error_ratio: list[float]
    remaining: list[float]
    burn_long: dict[str, list[float | None]]
    burn_short: dict[str, list[float | None]]
    multi: dict[str, list[bool]]
    naive: dict[str, list[bool]]


def simulate(
    slo_percent: float,
    window_days: float,
    incidents: tuple[Incident, ...] | list[Incident] | None = None,
) -> Simulation:
    """One-minute buckets across the window, then both alert policies.

    The multi-window alert fires only when the long and the short burn are
    both at or over the threshold, and only once the long window is full.
    The naive alert is the long window alone, same threshold. That is the
    comparison: same detection bar, much slower reset.
    """
    fraction = budget_fraction(slo_percent)
    window = period_seconds(window_days)
    n = int(window // STEP)
    if n < 1:
        raise ValueError("window is shorter than one minute")
    chosen = tuple(incidents) if incidents is not None else default_incidents(window_days)
    specs = alert_specs(window_days)
    widths = []
    for spec in specs:
        long_n = _window_buckets(spec.long_seconds)
        short_n = _window_buckets(spec.short_seconds)
        if short_n >= long_n and long_n > 1:
            short_n = long_n - 1
        widths.append((spec, long_n, short_n))

    ratios = [error_ratio_at(i * STEP, chosen, fraction) for i in range(n)]
    prefix = [0.0] * (n + 1)
    for i, ratio in enumerate(ratios):
        prefix[i + 1] = prefix[i] + ratio

    if fraction == 0.0:
        remaining = [0.0 if prefix[i + 1] == 0.0 else float("-inf") for i in range(n)]
    else:
        denom = fraction * n
        remaining = [1.0 - (prefix[i + 1] / denom) for i in range(n)]

    burn_long: dict[str, list[float | None]] = {}
    burn_short: dict[str, list[float | None]] = {}
    multi: dict[str, list[bool]] = {}
    naive: dict[str, list[bool]] = {}
    for spec, long_n, short_n in widths:
        long_series: list[float | None] = [None] * n
        short_series: list[float | None] = [None] * n
        multi_flags = [False] * n
        naive_flags = [False] * n
        threshold = spec.threshold
        for i in range(n):
            filled = i + 1
            long_burn = None
            if filled >= long_n:
                mean = (prefix[i + 1] - prefix[i + 1 - long_n]) / long_n
                long_burn = _burn(mean, fraction)
                long_series[i] = long_burn
                naive_flags[i] = long_burn >= threshold
            if filled >= short_n:
                mean_s = (prefix[i + 1] - prefix[i + 1 - short_n]) / short_n
                short_burn = _burn(mean_s, fraction)
                short_series[i] = short_burn
                if long_burn is not None and long_burn >= threshold and short_burn >= threshold:
                    multi_flags[i] = True
        burn_long[spec.id] = long_series
        burn_short[spec.id] = short_series
        multi[spec.id] = multi_flags
        naive[spec.id] = naive_flags

    return Simulation(
        step=STEP,
        window_seconds=window,
        budget_fraction=fraction,
        specs=specs,
        incidents=chosen,
        error_ratio=ratios,
        remaining=remaining,
        burn_long=burn_long,
        burn_short=burn_short,
        multi=multi,
        naive=naive,
    )


def sample_at(sim: Simulation, t: float) -> dict:
    """Bucket containing time ``t`` (clamped to the window)."""
    if not sim.error_ratio:
        raise ValueError("simulation is empty")
    index = int(float(t) // sim.step)
    if index < 0:
        index = 0
    if index >= len(sim.error_ratio):
        index = len(sim.error_ratio) - 1
    alerts = {}
    for spec in sim.specs:
        alerts[spec.id] = {
            "multi": sim.multi[spec.id][index],
            "naive": sim.naive[spec.id][index],
            "long": sim.burn_long[spec.id][index],
            "short": sim.burn_short[spec.id][index],
        }
    return {
        "index": index,
        "t": index * sim.step,
        "error_ratio": sim.error_ratio[index],
        "remaining": sim.remaining[index],
        "alerts": alerts,
    }


def flag_spans(flags: list[bool], step: float) -> list[tuple[float, float]]:
    """Closed-open ``[start, end)`` seconds where ``flags`` is true."""
    spans: list[tuple[float, float]] = []
    start = None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            spans.append((start * step, index * step))
            start = None
    if start is not None:
        spans.append((start * step, len(flags) * step))
    return spans


def incident_spans(sim: Simulation) -> list[tuple[float, float]]:
    return flag_spans([ratio > 0.0 for ratio in sim.error_ratio], sim.step)


@dataclass(frozen=True)
class PolicyScore:
    policy: str
    kind: str
    label: str
    precision: float | None
    recall: float | None
    detection_seconds: float | None
    reset_seconds: float | None
    detections: int
    misses: int
    alert_spans: int


def _score(sim: Simulation, spec: AlertSpec, kind: str, flags: list[bool]) -> PolicyScore:
    incident = [ratio > 0.0 for ratio in sim.error_ratio]
    true_positive = false_positive = false_negative = 0
    for is_bad, is_alert in zip(incident, flags):
        if is_alert and is_bad:
            true_positive += 1
        elif is_alert and not is_bad:
            false_positive += 1
        elif is_bad and not is_alert:
            false_negative += 1
    predicted = true_positive + false_positive
    actual = true_positive + false_negative
    precision = true_positive / predicted if predicted else None
    recall = true_positive / actual if actual else None

    detections: list[float] = []
    resets: list[float] = []
    misses = 0
    step = sim.step
    for start, end in incident_spans(sim):
        start_i = int(start // step)
        end_i = int(end // step)
        fired = None
        for index in range(start_i, end_i):
            if flags[index]:
                fired = index
                break
        if fired is None:
            misses += 1
            continue
        detections.append((fired - start_i) * step)
        cursor = end_i
        while cursor < len(flags) and flags[cursor]:
            cursor += 1
        resets.append((cursor - end_i) * step)

    label = f"{spec.name} · {'multi-window' if kind == 'multi' else 'long window only'}"
    return PolicyScore(
        policy=spec.id,
        kind=kind,
        label=label,
        precision=precision,
        recall=recall,
        detection_seconds=(sum(detections) / len(detections)) if detections else None,
        reset_seconds=(sum(resets) / len(resets)) if resets else None,
        detections=len(detections),
        misses=misses,
        alert_spans=len(flag_spans(flags, step)),
    )


def compare(sim: Simulation) -> list[PolicyScore]:
    """Precision, recall, detection delay, and reset delay for each policy."""
    scores = []
    for spec in sim.specs:
        scores.append(_score(sim, spec, "multi", sim.multi[spec.id]))
        scores.append(_score(sim, spec, "naive", sim.naive[spec.id]))
    return scores


def _clean_float(value: str, low: float, high: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number < low or number > high:
        return None
    return number


def share_query(state: dict) -> str:
    """Query string (no leading ``?``) for the calculator state."""
    incidents = state.get("incidents", list(INCIDENT_KINDS))
    if isinstance(incidents, str):
        incident_text = incidents
    else:
        incident_text = ",".join(name for name in incidents if name in INCIDENT_KINDS)
    pairs = {
        "sli": state.get("sli", "requests"),
        "slo": state.get("slo", 99.9),
        "window": state.get("window", 30),
        "rps": state.get("rps", 100),
        "bad": state.get("bad", 0),
        "elapsed": state.get("elapsed", 86400),
        "unit": state.get("unit", "days"),
        "incidents": incident_text,
        "t": state.get("t", 1),
    }
    return urlencode(pairs)


def parse_share(query: str) -> dict:
    """Validated fields present in ``query``. Unknown or illegal values are dropped."""
    raw = parse_qs(query.lstrip("?"), keep_blank_values=True)
    parsed: dict = {}
    if "sli" in raw and raw["sli"][0] in ("requests", "time"):
        parsed["sli"] = raw["sli"][0]
    if "slo" in raw:
        slo = _clean_float(raw["slo"][0], 0.0, 100.0)
        if slo is not None:
            parsed["slo"] = slo
    if "window" in raw:
        window = _clean_float(raw["window"][0], 1, 3650)
        if window is not None and int(window) in WINDOWS:
            parsed["window"] = int(window)
    if "rps" in raw:
        rps = _clean_float(raw["rps"][0], 0.0, 1e7)
        if rps is not None:
            parsed["rps"] = rps
    if "bad" in raw:
        bad = _clean_float(raw["bad"][0], 0.0, 1e18)
        if bad is not None:
            parsed["bad"] = bad
    if "elapsed" in raw:
        elapsed = _clean_float(raw["elapsed"][0], 0.0, 10 * 365 * 86400)
        if elapsed is not None:
            parsed["elapsed"] = elapsed
    if "unit" in raw and raw["unit"][0] in ("minutes", "hours", "days"):
        parsed["unit"] = raw["unit"][0]
    if "incidents" in raw:
        names = []
        for part in raw["incidents"][0].split(","):
            part = part.strip().lower()
            if part in INCIDENT_KINDS and part not in names:
                names.append(part)
        parsed["incidents"] = names
    if "t" in raw:
        position = _clean_float(raw["t"][0], 0.0, 1.0)
        if position is not None:
            parsed["t"] = position
    return parsed
