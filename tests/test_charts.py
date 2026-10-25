"""SVG chart tests. No browser."""

import xml.etree.ElementTree as ET

from charts import DARK, LIGHT, burn_rate_svg, burndown_svg, scenario_card_svg, timeline_svg

SVG = "{http://www.w3.org/2000/svg}"


def _root(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def test_burndown_is_well_formed_and_escapes():
    svg = burndown_svg([1.0, 0.5, 0.0, -0.2], 86400, LIGHT, title='Budget <left> & "go"')
    root = _root(svg)
    assert root.tag == f"{SVG}svg"
    assert root.find(f"{SVG}title").text == 'Budget <left> & "go"'
    assert "<left>" not in svg
    assert "&lt;left&gt;" in svg
    assert "100%" in svg
    assert svg.count("L") >= 1


def test_burndown_downsamples_long_series_and_survives_empty():
    remaining = [1 - index / 5000 for index in range(5000)]
    svg = burndown_svg(remaining, 30 * 86400, DARK)
    assert svg.count("L") < 600
    _root(svg)
    empty = burndown_svg([], 3600, LIGHT)
    assert "No budget samples" in empty
    _root(empty)


def test_burn_rate_draws_thresholds_on_a_log_scale():
    series = {
        "fast": [None, 0, 16.7, 400, 0],
        "slow": [None, 0, 2, 8, 8],
        "ticket": [0.2, 0.4, 1, 1, 1],
    }
    svg = burn_rate_svg(series, {"fast": 14.4, "slow": 6, "ticket": 1}, 7 * 86400, LIGHT, title="Burn rate")
    root = _root(svg)
    assert root.find(f"{SVG}title").text == "Burn rate"
    assert 'stroke-dasharray="4 4"' in svg
    assert svg.count("<path ") == 3


def test_timeline_lanes_and_card():
    lanes = [
        {"label": "Errors", "spans": [(100, 400)], "color": LIGHT["accent"], "hatched": False},
        {"label": "Fast, long only", "spans": [(100, 900)], "color": LIGHT["naive"], "hatched": True},
    ]
    svg = timeline_svg(3600, lanes, LIGHT, title="Alerts & resets")
    _root(svg)
    assert "Fast, long only" in svg
    assert "url(#hatch)" in svg
    card = scenario_card_svg([1, 0.8, 0.2], 30 * 86400, LIGHT)
    root = _root(card)
    assert root.get("viewBox") == "0 0 1200 750"
