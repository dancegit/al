# Notes

A one-page notes app: a list of notes and a form to add one. Flask, one template, one stylesheet,
notes kept in memory.

## Layout

| Path | What it is |
|---|---|
| `app.py` | the Flask app: `create_app()`, routes `GET /` (the page) and `POST /notes` (add a note) |
| `templates/index.html` | the only page |
| `static/style.css` | all styling (colours, layout) |
| `tests/test_app.py` | unit tests with the Flask test client |
| `e2e/scenario.py` | the user scenario walked in a real browser: every user-visible behaviour is demonstrated here |
| `e2e/capture.py` | runs the scenario in headless Chromium, saves screenshots and a video to `e2e-results/` |

## Commands

```bash
pip install -r requirements.txt && playwright install chromium   # once
python app.py                     # http://127.0.0.1:8000
python -m pytest -q tests         # unit tests
python e2e/capture.py             # end-to-end: e2e-results/NN-*.png and e2e-results/session.webm
```

## Rules

- A change a user can see is demonstrated in `e2e/scenario.py`, so the screenshots and the video show it.
- Expectations in `tests/` and `e2e/scenario.py` are never loosened to make a change pass.
- `e2e/capture.py` is the test harness, not part of the app.
