"""Pure-function tests for error budgets and burn-rate alerts."""

import math

import pytest

from slo import (
    INCIDENT_KINDS,
    AlertSpec,
    Incident,
    alert_specs,
    allowance_summary,
    allowed_bad_requests,
    allowed_downtime,
    budget_fraction,
    budget_status,
    compare,
    default_incidents,
    error_ratio_at,
    flag_spans,
    format_burn,
    format_count,
    format_duration,
    format_slo,
    incident_spans,
    nines_table,
    parse_share,
    sample_at,
    share_query,
    simulate,
    status_summary,
)


def test_budget_fraction_and_classic_downtime():
    assert budget_fraction(99.9) == pytest.approx(0.001)
    assert budget_fraction(100) == 0
    assert budget_fraction(0) == 1
    # 99.9% of a month is 43.2 minutes, and 86.4 seconds a day.
    assert allowed_downtime(99.9, 30) == pytest.approx(2592)
    assert allowed_downtime(99.9, 1) == pytest.approx(86.4)
    assert allowed_downtime(99, 1) == pytest.approx(864)
    assert allowed_bad_requests(99.9, 30, 100) == pytest.approx(259_200)


def test_budget_fraction_rejects_out_of_range():
    with pytest.raises(ValueError):
        budget_fraction(100.1)
    with pytest.raises(ValueError):
        budget_fraction(-1)
    with pytest.raises(ValueError):
        allowed_downtime(99.9, 0)
    with pytest.raises(ValueError):
        allowed_bad_requests(99.9, 30, -1)


def test_formats():
    assert format_slo(99) == "99%"
    assert format_slo(99.9) == "99.9%"
    assert format_slo(99.95) == "99.95%"
    assert format_duration(2592) == "43.2 min"
    assert format_duration(86.4) == "86.4 s"
    assert format_duration(86400) == "1 day"
    assert format_duration(30 * 86400) == "30 days"
    assert format_duration(3600) == "1 hour"
    assert format_duration(300) == "5 min"
    assert format_duration(None) == "—"
    assert format_count(259200) == "259,200"
    assert format_count(0) == "0"
    assert format_burn(1) == "1×"
    assert format_burn(6) == "6×"
    assert format_burn(14.4) == "14.4×"
    assert format_burn(None) == "—"


def test_on_pace_burn_is_one_and_exhausts_with_the_window():
    allowed = allowed_bad_requests(99.9, 30, 100)
    status = budget_status(99.9, 30, bad=allowed / 30, elapsed_seconds=86400, rps=100, sli="requests")
    assert status.consumed == pytest.approx(1 / 30)
    assert status.remaining == pytest.approx(29 / 30)
    assert status.burn_rate == pytest.approx(1)
    assert status.seconds_to_exhaustion == pytest.approx(29 * 86400)
    assert "1×" in status_summary(status)
    assert "29 days" in status_summary(status)

    time_status = budget_status(
        99.9, 30, bad=allowed_downtime(99.9, 30) / 30, elapsed_seconds=86400, rps=100, sli="time"
    )
    assert time_status.burn_rate == pytest.approx(1)


def test_budget_edges():
    untouched = budget_status(99.9, 30, bad=0, elapsed_seconds=86400, rps=50, sli="requests")
    assert untouched.burn_rate == 0
    assert untouched.seconds_to_exhaustion is None
    assert "0×" in status_summary(untouched)

    unopened = budget_status(99.9, 30, bad=10, elapsed_seconds=0, rps=50, sli="requests")
    assert unopened.burn_rate is None
    assert "Enter how long" in status_summary(unopened)

    perfect = budget_status(100, 30, bad=0, elapsed_seconds=60, rps=10, sli="requests")
    assert perfect.consumed == 0
    assert perfect.burn_rate == 0

    broken = budget_status(100, 30, bad=1, elapsed_seconds=60, rps=10, sli="requests")
    assert math.isinf(broken.consumed)
    assert broken.seconds_to_exhaustion == 0
    assert "exhausted" in status_summary(broken)

    spent = budget_status(99.9, 30, bad=allowed_downtime(99.9, 30) + 10, elapsed_seconds=86400, rps=1, sli="time")
    assert spent.consumed > 1
    assert spent.seconds_to_exhaustion == 0

    with pytest.raises(ValueError):
        budget_status(99.9, 30, bad=-1, elapsed_seconds=10, rps=1, sli="requests")
    with pytest.raises(ValueError):
        budget_status(99.9, 30, bad=0, elapsed_seconds=10, rps=1, sli="latency")


def test_nines_table_marks_the_selected_target_and_inserts_custom():
    rows = nines_table(30, 100, highlight=99.9)
    labels = [row.label for row in rows]
    assert "99.9%" in labels
    current = [row for row in rows if row.current]
    assert len(current) == 1
    assert current[0].downtime_day == pytest.approx(86.4)
    custom = nines_table(7, 10, highlight=99.2)
    assert any(row.current and row.label == "99.2%" for row in custom)
    assert allowance_summary(99.9, 30, 100).startswith("A 99.9% SLO over 30 days")


def test_workbook_windows_are_exact_at_30_days_and_scale_otherwise():
    specs = {spec.id: spec for spec in alert_specs(30)}
    assert specs["fast"] == AlertSpec("fast", "Fast burn", "page", 14.4, 3600, 300)
    assert specs["slow"] == AlertSpec("slow", "Slow burn", "page", 6, 21600, 1800)
    assert specs["ticket"] == AlertSpec("ticket", "Ticket", "ticket", 1, 259200, 21600)
    assert specs["fast"].budget_slice(30 * 86400) == pytest.approx(0.02)
    assert specs["slow"].budget_slice(30 * 86400) == pytest.approx(0.05)
    assert specs["ticket"].budget_slice(30 * 86400) == pytest.approx(0.10)

    scaled = {spec.id: spec for spec in alert_specs(7)}
    assert scaled["fast"].threshold == 14.4
    assert scaled["fast"].long_seconds == pytest.approx(3600 * 7 / 30)
    assert scaled["fast"].short_seconds == pytest.approx(300 * 7 / 30)
    assert scaled["fast"].budget_slice(7 * 86400) == pytest.approx(0.02)
    assert scaled["ticket"].long_seconds < 7 * 86400


def test_error_ratio_overlap_degradation_and_flap():
    fraction = 0.001
    assert error_ratio_at(0, [Incident("degradation", 0, 100, peak=8)], fraction) == pytest.approx(0.008)
    assert error_ratio_at(0, [Incident("degradation", 0, 100, peak=8)], 0.5) == 1.0
    both = [
        Incident("degradation", 0, 100, peak=8),
        Incident("outage", 0, 50, peak=1),
    ]
    assert error_ratio_at(10, both, fraction) == 1.0
    assert error_ratio_at(60, both, fraction) == pytest.approx(0.008)
    flap = Incident("flap", 0, 10_000, peak=1, on=180, off=720)
    assert error_ratio_at(0, [flap], fraction) == 1
    assert error_ratio_at(120, [flap], fraction) == 1
    assert error_ratio_at(180, [flap], fraction) == 0
    assert error_ratio_at(900, [flap], fraction) == 1
    with pytest.raises(ValueError):
        error_ratio_at(0, [Incident("mystery", 0, 10)], fraction)


def test_total_outage_pages_immediately_and_the_short_window_resets():
    start = 5 * 86400
    sim = simulate(99.9, 30, [Incident("outage", start, 25 * 60)])
    during = sample_at(sim, start)
    assert during["alerts"]["fast"]["multi"] is True
    assert during["alerts"]["fast"]["long"] == pytest.approx(1000 / 60)
    assert during["alerts"]["fast"]["short"] == pytest.approx(200)
    assert sample_at(sim, start - 60)["alerts"]["fast"]["multi"] is False

    # Six minutes after recovery the 5-minute window is clean; the 1-hour window is not.
    recovered = start + 25 * 60 + 6 * 60
    after = sample_at(sim, recovered)
    assert after["alerts"]["fast"]["multi"] is False
    assert after["alerts"]["fast"]["naive"] is True

    scores = {(score.policy, score.kind): score for score in compare(sim)}
    fast_multi = scores[("fast", "multi")]
    fast_naive = scores[("fast", "naive")]
    assert fast_multi.detection_seconds == pytest.approx(0)
    assert fast_multi.reset_seconds == pytest.approx(240, abs=60)
    assert fast_naive.reset_seconds == pytest.approx(3540, abs=120)
    assert fast_multi.reset_seconds < fast_naive.reset_seconds / 5
    assert fast_multi.precision > fast_naive.precision


def test_eight_x_degradation_trips_slow_burn_not_fast_page():
    start = 2 * 86400
    sim = simulate(99.9, 30, [Incident("degradation", start, 12 * 3600, peak=8)])
    assert not any(sim.multi["fast"])
    assert any(sim.multi["slow"])
    scores = {(score.policy, score.kind): score for score in compare(sim)}
    slow = scores[("slow", "multi")]
    # 4.5h of an 8× burn fills a 6h window to exactly 6×. Off-by-one bucket is one minute.
    assert slow.detection_seconds == pytest.approx(4.5 * 3600 - 60, abs=90)
    assert slow.misses == 0
    assert scores[("fast", "multi")].misses == 1
    assert scores[("slow", "naive")].reset_seconds > slow.reset_seconds


def test_default_scenario_tells_the_three_stories():
    sim = simulate(99.9, 30, default_incidents(30))
    assert len(sim.error_ratio) == 30 * 86400 // 60
    spans = incident_spans(sim)
    assert len(spans) > 3  # the flap is many separate bursts
    scores = {(score.policy, score.kind): score for score in compare(sim)}
    fast_multi = scores[("fast", "multi")]
    # The 12-hour degradation is the one incident a 14.4× page should ignore.
    assert fast_multi.misses == 1
    assert fast_multi.detections == 25  # the outage, plus each flap
    assert fast_multi.precision > scores[("fast", "naive")].precision
    assert fast_multi.reset_seconds < scores[("fast", "naive")].reset_seconds
    # The 6× page does catch that slow degradation, so it covers more bad minutes.
    assert scores[("slow", "multi")].recall > fast_multi.recall
    assert scores[("slow", "multi")].misses == 0


def test_clean_window_does_not_alert_and_empty_scores_are_none():
    sim = simulate(99.9, 7, [])
    assert all(ratio == 0 for ratio in sim.error_ratio)
    assert not any(any(flags) for flags in sim.multi.values())
    scores = compare(sim)
    assert all(score.precision is None and score.recall is None for score in scores)
    assert flag_spans([False, True, True, False], 60) == [(60, 180)]


def test_slo_of_100_percent_alerts_on_any_error():
    sim = simulate(100, 7, [Incident("outage", 86400, 120)])
    sample = sample_at(sim, 86400)
    assert math.isinf(sample["alerts"]["fast"]["long"])
    assert sample["alerts"]["fast"]["multi"] is True
    assert sample_at(sim, 0)["alerts"]["fast"]["multi"] is False


def test_share_query_round_trip_and_rejects_junk():
    state = {
        "sli": "time",
        "slo": 99.95,
        "window": 7,
        "rps": 12.5,
        "bad": 3,
        "elapsed": 3600,
        "unit": "hours",
        "incidents": ["outage", "flap"],
        "t": 0.25,
    }
    parsed = parse_share("?" + share_query(state))
    assert parsed["sli"] == "time"
    assert parsed["slo"] == pytest.approx(99.95)
    assert parsed["window"] == 7
    assert parsed["rps"] == pytest.approx(12.5)
    assert parsed["bad"] == pytest.approx(3)
    assert parsed["elapsed"] == pytest.approx(3600)
    assert parsed["unit"] == "hours"
    assert parsed["incidents"] == ["outage", "flap"]
    assert parsed["t"] == pytest.approx(0.25)

    junk = parse_share("sli=latency&slo=101&window=15&rps=-4&incidents=outage,nope,flap&t=9&elapsed=nope")
    assert "sli" not in junk
    assert "slo" not in junk
    assert "window" not in junk
    assert junk["incidents"] == ["outage", "flap"]
    assert "t" not in junk
    assert set(INCIDENT_KINDS) == {"outage", "degradation", "flap"}


def test_sample_clamps_and_default_incidents_fit_every_preset_window():
    sim = simulate(99.5, 28, [])
    assert sample_at(sim, -100)["index"] == 0
    assert sample_at(sim, 10**12)["t"] == sim.window_seconds - sim.step
    for days in (7, 28, 30, 90):
        incidents = default_incidents(days)
        assert [item.kind for item in incidents] == ["outage", "degradation", "flap"]
        window = days * 86400
        for item in incidents:
            assert 0 <= item.start < window
            assert item.start + item.duration <= window + 1e-6
