# Panel harness

Runs the real `main.js` from the built plugin against fake Premiere/UXP APIs, a mocked Claude API and
the **real** engine (which processes the job files). It checks the API key test and save flow, engine
start, clip reading and flags, the Claude hand-off, results, LUT path copy, approvals and the report.

```bash
python packaging/build_ccx.py
python -m tests.fixtures /tmp/footage
cd tests/panel && npm install jsdom@24.1.3
node harness.js ../../build/ConsistentGrade /tmp/cg-home /tmp/footage   # prints HARNESS OK
```

It does not prove behaviour inside Premiere: Spectrum widgets, consent dialogs and file: image URLs are
only real in the host. See docs/ACCEPTANCE.md.
