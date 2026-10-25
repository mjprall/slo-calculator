"""SVG charts for the error-budget simulation.

Pure Python. Colors are passed in so this module never reads the DOM.
The browser glue drops the returned strings into the page.
"""

from __future__ import annotations

import math
from typing import Sequence

LIGHT = {
    "ink": "#1c1916",
    "muted": "#5c564c",
    "line": "#ddd4c6",
    "grid": "#e6dfd2",
    "accent": "#8d3d22",
    "enamel": "#1d3f3b",
    "surface": "#fbf8f3",
    "fast": "#8d3d22",
    "slow": "#8a5a12",
    "ticket": "#1d3f3b",
    "naive": "#8d6848",
}

DARK = {
    "ink": "#f4efe6",
    "muted": "#c8c0b4",
    "line": "#34362f",
    "grid": "#3a3c35",
    "accent": "#f0b59a",
    "enamel": "#9ed9ce",
    "surface": "#1c1e1b",
    "fast": "#f0b59a",
    "slow": "#e6c27a",
    "ticket": "#9ed9ce",
    "naive": "#b9a08a",
}

# Plot box shared with the HTML cursor overlay. Change both together.
PLOT_W = 720
PLOT_H = 280
PAD_L = 58
PAD_R = 18
PAD_T = 18
PAD_B = 36
TIMELINE_LABEL_W = 156


def cursor_fraction(
    t: float,
    t0: float,
    t1: float,
    width: float = PLOT_W,
    left: float = PAD_L,
    right: float = PAD_R,
) -> float:
    """Where a vertical cursor sits, as a fraction of the SVG width."""
    if t1 <= t0:
        return left / width
    span = width - left - right
    x = left + span * ((t - t0) / (t1 - t0))
    return min(1.0, max(0.0, x / width))


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _pool(values: Sequence[float | None], bins: int, mode: str) -> list[float | None]:
    n = len(values)
    if n == 0:
        return []
    if n <= bins:
        return list(values)
    pooled: list[float | None] = []
    for bin_index in range(bins):
        start = int(bin_index * n / bins)
        end = int((bin_index + 1) * n / bins)
        if end <= start:
            end = start + 1
        chunk = [value for value in values[start:end] if value is not None and not math.isnan(value)]
        if not chunk:
            pooled.append(None)
        elif mode == "min":
            pooled.append(min(chunk))
        else:
            pooled.append(max(chunk))
    return pooled


def _times_for(n: int, bins: int, t1: float) -> list[float]:
    count = n if n <= bins else bins
    if count <= 1:
        return [0.0]
    return [t1 * index / (count - 1) for index in range(count)]


def _map_x(t: float, t1: float, width: float, left: float, right: float) -> float:
    if t1 <= 0:
        return left
    return left + (width - left - right) * (t / t1)


def _polyline(points: list[tuple[float, float]]) -> str:
    if len(points) < 2:
        return ""
    parts = [f"M{points[0][0]:.1f} {points[0][1]:.1f}"]
    parts.extend(f"L{x:.1f} {y:.1f}" for x, y in points[1:])
    return " ".join(parts)


def _split_polyline(xs: list[float], ys: list[float | None]) -> str:
    paths = []
    current: list[tuple[float, float]] = []
    for x, y in zip(xs, ys):
        if y is None or math.isnan(y):
            if len(current) >= 2:
                paths.append(_polyline(current))
            current = []
            continue
        current.append((x, y))
    if len(current) >= 2:
        paths.append(_polyline(current))
    return " ".join(paths)


def _frame(title: str, width: float, height: float, colors: dict, body: str, background: str | None) -> str:
    bg = ""
    if background:
        bg = f'<rect width="{width:.0f}" height="{height:.0f}" fill="{_esc(background)}"/>'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="100%" role="img" aria-label="{_esc(title)}">'
        f"<title>{_esc(title)}</title>"
        f"{bg}{body}</svg>"
    )


def _day_ticks(t1: float) -> list[tuple[float, str]]:
    if t1 <= 0:
        return []
    days = t1 / 86400.0
    if days <= 2:
        step = 6 * 3600
        label = lambda t: f"{int(round(t / 3600))}h"
    elif days <= 14:
        step = 86400
        label = lambda t: f"{int(round(t / 86400))}d"
    else:
        step = 7 * 86400 if days > 40 else 5 * 86400
        label = lambda t: f"{int(round(t / 86400))}d"
    ticks = [(0.0, "0")]
    t = step
    while t < t1 - step * 0.25:
        ticks.append((t, label(t)))
        t += step
    ticks.append((t1, label(t1)))
    return ticks


def burndown_svg(
    remaining: Sequence[float | None],
    window_seconds: float,
    colors: dict,
    title: str = "Error budget remaining",
    width: float = PLOT_W,
    height: float = PLOT_H,
    background: str | None = None,
    max_points: int = 480,
) -> str:
    """Remaining budget (1 is full, 0 is exhausted, negative is overspent)."""
    pooled = _pool(remaining, max_points, "min")
    xs_t = _times_for(len(remaining), max_points, window_seconds)
    finite = [value for value in pooled if value is not None and not math.isinf(value)]
    lo = min(0.0, min(finite) if finite else 0.0)
    hi = max(1.0, max(finite) if finite else 1.0)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    pad = (hi - lo) * 0.06
    lo -= pad
    hi += pad

    def y(value: float) -> float:
        return PAD_T + (height - PAD_T - PAD_B) * (1.0 - (value - lo) / (hi - lo))

    parts = []
    axis = colors["muted"]
    for frac, text in ((1.0, "100%"), (0.0, "0")):
        if lo <= frac <= hi:
            yy = y(frac)
            parts.append(
                f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{width - PAD_R}" y2="{yy:.1f}" '
                f'stroke="{colors["grid"]}" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{PAD_L - 8}" y="{yy + 4:.1f}" text-anchor="end" '
                f'fill="{axis}" font-size="12" font-family="IBM Plex Sans, sans-serif">{text}</text>'
            )
    if finite and min(finite) < -0.05:
        low = min(finite)
        yy = y(low)
        parts.append(
            f'<text x="{PAD_L - 8}" y="{yy + 4:.1f}" text-anchor="end" '
            f'fill="{axis}" font-size="12" font-family="IBM Plex Sans, sans-serif">{low * 100:.0f}%</text>'
        )
    for t, text in _day_ticks(window_seconds):
        x = _map_x(t, window_seconds, width, PAD_L, PAD_R)
        parts.append(
            f'<text x="{x:.1f}" y="{height - 12:.1f}" text-anchor="middle" '
            f'fill="{axis}" font-size="12" font-family="IBM Plex Sans, sans-serif">{_esc(text)}</text>'
        )
    ys = [None if value is None or math.isinf(value) else y(value) for value in pooled]
    xs = [_map_x(t, window_seconds, width, PAD_L, PAD_R) for t in xs_t]
    path = _split_polyline(xs, ys)
    if path:
        parts.append(
            f'<path d="{path}" fill="none" stroke="{colors["enamel"]}" stroke-width="2.25" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
    else:
        parts.append(
            f'<text x="{width / 2:.0f}" y="{height / 2:.0f}" text-anchor="middle" '
            f'fill="{axis}" font-size="14" font-family="IBM Plex Sans, sans-serif">No budget samples</text>'
        )
    return _frame(title, width, height, colors, "".join(parts), background)


def burn_rate_svg(
    series: dict[str, Sequence[float | None]],
    thresholds: dict[str, float],
    window_seconds: float,
    colors: dict,
    title: str = "Burn rate",
    width: float = PLOT_W,
    height: float = PLOT_H,
    background: str | None = None,
    max_points: int = 480,
) -> str:
    """Long-window burn rates on a log axis, with threshold guides."""
    order = [key for key in ("fast", "slow", "ticket") if key in series]
    order.extend(key for key in series if key not in order)
    pooled = {key: _pool(series[key], max_points, "max") for key in order}
    xs_t = _times_for(max((len(values) for values in series.values()), default=0), max_points, window_seconds)
    peak = 1.0
    for values in pooled.values():
        for value in values:
            if value is not None and not math.isnan(value) and not math.isinf(value):
                peak = max(peak, value)
    for value in thresholds.values():
        peak = max(peak, value)
    floor = 0.05
    ceiling = max(20.0, peak * 1.15)

    def y(value: float) -> float:
        clamped = min(ceiling, max(floor, value))
        span = math.log(ceiling) - math.log(floor)
        return PAD_T + (height - PAD_T - PAD_B) * (1.0 - (math.log(clamped) - math.log(floor)) / span)

    parts = []
    axis = colors["muted"]
    guides = [0.1, 1.0, 10.0, 100.0]
    if ceiling > 500:
        guides.append(1000.0)
    for guide in guides:
        if floor <= guide <= ceiling:
            yy = y(guide)
            parts.append(
                f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{width - PAD_R}" y2="{yy:.1f}" '
                f'stroke="{colors["grid"]}" stroke-width="1"/>'
            )
            label = f"{guide:.0f}×" if guide >= 1 else f"{guide:g}×"
            parts.append(
                f'<text x="{PAD_L - 8}" y="{yy + 4:.1f}" text-anchor="end" fill="{axis}" '
                f'font-size="12" font-family="IBM Plex Sans, sans-serif">{label}</text>'
            )
    seen = set()
    for key, threshold in thresholds.items():
        rounded = round(threshold, 2)
        if rounded in seen or not floor <= threshold <= ceiling:
            continue
        seen.add(rounded)
        yy = y(threshold)
        color = colors.get(key, colors["accent"])
        parts.append(
            f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{width - PAD_R}" y2="{yy:.1f}" '
            f'stroke="{color}" stroke-width="1" stroke-dasharray="4 4" opacity="0.85"/>'
        )
    for t, text in _day_ticks(window_seconds):
        x = _map_x(t, window_seconds, width, PAD_L, PAD_R)
        parts.append(
            f'<text x="{x:.1f}" y="{height - 12:.1f}" text-anchor="middle" fill="{axis}" '
            f'font-size="12" font-family="IBM Plex Sans, sans-serif">{_esc(text)}</text>'
        )
    xs = [_map_x(t, window_seconds, width, PAD_L, PAD_R) for t in xs_t]
    for key in order:
        ys = []
        for value in pooled[key]:
            if value is None or math.isnan(value):
                ys.append(None)
            elif math.isinf(value):
                ys.append(y(ceiling))
            else:
                ys.append(y(value))
        path = _split_polyline(xs, ys)
        if path:
            parts.append(
                f'<path d="{path}" fill="none" stroke="{colors.get(key, colors["ink"])}" '
                f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            )
    return _frame(title, width, height, colors, "".join(parts), background)


def timeline_svg(
    window_seconds: float,
    lanes: Sequence[dict],
    colors: dict,
    title: str = "Alert timeline",
    width: float = PLOT_W,
    background: str | None = None,
) -> str:
    """Horizontal lanes. Each lane is ``{label, spans, color, hatched}``.

    Spans are closed-open ``(start, end)`` in seconds. Hatched lanes are the
    naive long-window alerts, so the pattern is not color alone.
    """
    lane_h = 22
    label_w = TIMELINE_LABEL_W
    top = 12
    height = top + max(1, len(lanes)) * lane_h + 28
    parts = [
        f'<clipPath id="lanes"><rect x="{label_w}" y="0" width="{width - label_w - 8}" height="{height}"/></clipPath>'
    ]
    parts.append(
        "<pattern id=\"hatch\" width=\"6\" height=\"6\" patternUnits=\"userSpaceOnUse\">"
        '<path d="M0 6 L6 0" stroke="#000" stroke-width="1" opacity="0.55"/>'
        "</pattern>"
    )
    for index, lane in enumerate(lanes):
        y = top + index * lane_h
        color = lane.get("color", colors["accent"])
        parts.append(
            f'<text x="{label_w - 8}" y="{y + 15:.1f}" text-anchor="end" fill="{colors["ink"]}" '
            f'font-size="12" font-family="IBM Plex Sans, sans-serif">{_esc(lane.get("label", ""))}</text>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y + 4:.1f}" width="{width - label_w - 10:.1f}" height="14" '
            f'fill="{colors["grid"]}" rx="3"/>'
        )
        for start, end in lane.get("spans", ()):
            if end <= start or window_seconds <= 0:
                continue
            x = label_w + (width - label_w - 10) * (max(0.0, start) / window_seconds)
            w = (width - label_w - 10) * (min(window_seconds, end) - max(0.0, start)) / window_seconds
            if w < 1.2:
                w = 1.2
            if lane.get("hatched"):
                parts.append(
                    f'<g clip-path="url(#lanes)"><rect x="{x:.1f}" y="{y + 4:.1f}" width="{w:.1f}" height="14" '
                    f'fill="{color}" opacity="0.35"/>'
                    f'<rect x="{x:.1f}" y="{y + 4:.1f}" width="{w:.1f}" height="14" fill="url(#hatch)"/></g>'
                )
            else:
                parts.append(
                    f'<rect x="{x:.1f}" y="{y + 4:.1f}" width="{w:.1f}" height="14" fill="{color}" '
                    f'clip-path="url(#lanes)" rx="2"/>'
                )
    for t, text in _day_ticks(window_seconds):
        x = label_w + (width - label_w - 10) * (t / window_seconds if window_seconds else 0)
        parts.append(
            f'<text x="{x:.1f}" y="{height - 8:.1f}" text-anchor="middle" fill="{colors["muted"]}" '
            f'font-size="12" font-family="IBM Plex Sans, sans-serif">{_esc(text)}</text>'
        )
    return _frame(title, width, height, colors, "".join(parts), background)


def scenario_card_svg(
    remaining: Sequence[float | None],
    window_seconds: float,
    colors: dict | None = None,
) -> str:
    """Landing-card image: the real burndown, framed, no extra decoration."""
    palette = colors or LIGHT
    chart = burndown_svg(
        remaining,
        window_seconds,
        palette,
        title="Error budget remaining for a 99.9% SLO over 30 days",
        width=1200,
        height=750,
        background=palette["surface"],
        max_points=360,
    )
    return chart
