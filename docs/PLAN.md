# Plan and milestones

## Decisions taken without input (please confirm)

| Question from the brief | Assumption used | Change if… |
|---|---|---|
| Operating system | either. The panel is UXP, the service is cross-platform | macOS + http is refused → use the TLS option |
| Premiere version | 25.6+ (UXP GA) | older → a CEP/ExtendScript panel is needed instead |
| Footage | SDR Rec.709 only | log/RAW → add OCIO input normalisation first |
| Matching or look | both. Matching first; the look is a separate, optional layer | — |
| Frames to cloud AI | **off by default**. Opt-in checkbox in the panel and `--ai api` in the CLI | — |

## Milestones

| # | Milestone | State | Reproduce |
|---|---|---|---|
| 1 | CLI → extraction → measurements → identity LUT → report | done | `python -m autograde analyze --input footage --output r` |
| 2 | Conservative correction → matching → graded previews | done, tested on synthetic footage | `pytest tests/test_pipeline.py tests/test_spec.py` |
| 3 | Optional AI interpretation (provider interface: Anthropic / file / prepare / mock) | done; **live API untested here** (no key in the build environment) | `--ai mock`, `--ai prepare`, `--ai api` |
| 4 | Premiere capability spike | panel capability test written; **not run on Premiere** | panel → Run capability test |
| 5 | Panel → service → export / guided apply | written; **not run on Premiere** | docs/ACCEPTANCE.md |

## Next steps

1. Run the acceptance test on your machine and fill in the capability matrix.
2. Test on real footage. Tune `DAMPING`, the neutral-candidate thresholds and the skin heuristic
   (`autograde/grade.py`, `autograde/measure.py`).
3. Optionally grade **rendered timeline appearance**: `Exporter.exportSequenceFrame` can export frames as
   Premiere shows them (with existing effects), as an alternative to clean source decoding.
4. Keyframed exposure for flagged shots (bounded and smoothed), applied through documented effect parameters
   if that turns out to be reliable.
5. Log inputs via OpenColorIO.
