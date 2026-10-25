# SLO and error budget calculator

Matthew Prall's SLO and error budget calculator, with burn-rate alerts.

**Live demo:** [https://mjprall.github.io/slo-calculator/](https://mjprall.github.io/slo-calculator/)

Part of [Matthew Prall's site](https://mjprall.github.io/).

An availability target is a budget of failure. This page turns an SLO into downtime and bad requests, then runs the multi-window burn-rate alerts from the [SRE workbook](https://sre.google/workbook/alerting-on-slos/) on a synthetic incident, next to the same threshold on the long window alone.

There is no build step. Python runs in the browser with [PyScript](https://pyscript.net/) 2026.7.3. `slo.py` is the budget math and the alert simulation, `charts.py` turns that into SVG, and `app.py` is the only module that touches the page.

## Features

- **Error budget calculator.** Pick an availability SLO, a window, and a request rate. The page shows the downtime the budget allows, per day and over the window, and the count of bad requests that would spend it.
- **Budget status.** Enter the bad requests or the downtime observed so far, and how much of the window has elapsed. The page reports how much of the budget is consumed, how much is left, the burn rate, and when the budget would run out if that rate held.
- **Multi-window, multi-burn-rate alert simulator.** Play a request stream with a sharp outage, a slow rise in errors, or a flapping fault. An alert fires only when a long window and a short window are both over the burn rate. The same threshold on the long window alone is drawn beside it, so you can see the naive alert stay open after the errors stop.

Lookbacks scale with the SLO window (`window / 30`). The burn multipliers stay 14.4×, 6×, and 1×. A 30-day window matches the workbook (1 hour / 5 minutes, 6 hours / 30 minutes, 3 days / 6 hours). Any other window still spends about 2%, 5%, and 10% of its budget.

Paths are relative (`styles.css`, `fonts/…`, `./app.py`), so the page works when GitHub Pages serves this project at `/slo-calculator/`.

Fonts are [Newsreader](fonts/NEWSREADER-OFL.txt) and [IBM Plex](fonts/IBM-PLEX-OFL.txt), both under the SIL Open Font License.

## Run locally

PyScript needs the files served over HTTP. Opening `index.html` from `file://` will not work.

From the repository root:

```bash
python3 -m http.server
```

Open `http://127.0.0.1:8000/`.

GitHub Pages serves this repository at `/slo-calculator/`. To preview that path, put the repo behind that prefix and serve the parent:

```bash
mkdir -p /tmp/slo-preview
ln -sfn "$PWD" /tmp/slo-preview/slo-calculator
python3 -m http.server -d /tmp/slo-preview 8000
```

Open `http://127.0.0.1:8000/slo-calculator/`.

## Tests

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -v
```

The suite covers `slo.py` and `charts.py` on plain CPython. It does not need a browser.

## Publish

One time: **Settings → Pages → Source: GitHub Actions**. That source is already selected for this repository.

[`.github/workflows/pages.yml`](.github/workflows/pages.yml) runs on every push to `main`, and when started by hand with **workflow_dispatch**. It runs pytest on Python 3.11, 3.12, and 3.13, then deploys with `actions/configure-pages`, `actions/upload-pages-artifact`, and `actions/deploy-pages`. The artifact is the site files only (`index.html`, styles, Python modules, fonts, favicon, and this README). `tests/` is not published.

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs the same pytest suite on pull requests.
