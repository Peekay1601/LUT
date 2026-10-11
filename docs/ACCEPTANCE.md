# Manual Premiere acceptance test

End-to-end parity with Premiere is **not yet verified**. Run this once per Premiere version and footage
type before trusting the LUTs, and record the result at the bottom.

## A. LUT matches the local preview

1. Run `python -m tests.fixtures ./footage`, then:
   `python -m autograde analyze --input ./footage --reference a_reference.mp4 --output ./results`
2. Import `footage/b_dark_green.mp4` into a new **Rec.709** sequence of the same size and frame rate.
   Turn off display colour management for the comparison, or leave it on consistently on both sides.
3. Apply Lumetri Color → Creative → Look → Browse… → `results/shot_002/grade.cube`.
4. Move the playhead to the time printed in the first sample's file name
   (`results/shot_002/samples/sample_01_<seconds>s.png`).
5. File › Export › Frame (PNG), then compare it with the right half of `results/shot_002/previews/ba_01.jpg`:
   ```bash
   python - <<'EOF'
   import numpy as np
   from PIL import Image
   a = np.asarray(Image.open("premiere_frame.png").convert("RGB").resize((640, 360))).astype(float)
   b = np.asarray(Image.open("results/shot_002/previews/ba_01.jpg").convert("RGB"))[22:, 646:].astype(float)
   d = np.abs(a - b[:a.shape[0], :a.shape[1]])
   print("mean", d.mean(), "p99", np.percentile(d, 99))
   EOF
   ```
   **Pass**: mean difference < 2 and p99 < 6 code values (JPEG and resampling account for most of this).
   A uniform offset, or a shift only in the shadows, usually means a range mismatch (limited vs full).
   Note the fixtures are tagged limited range.

## B. Panel workflow

1. `python -m autograde serve`, copy the printed token into the panel and click Save. Paste your Anthropic
   key, then Test key → Save key.
2. Select 3–5 clips → **Read selected clips**. Check that the source in/out values are source-media seconds
   (trim a clip's head and confirm `source_in` changes while `timeline_in` doesn't).
3. Speed-change one clip, nest one, and add Lumetri to one. All three should be flagged and skipped.
4. **Run capability test** and copy the table into the Results section below.
5. Analyse (Claude off), check the previews, apply one LUT by hand, and tick Approved. `approvals.json`
   should appear in the job folder.
6. Repeat with Claude on, after agreeing that frames may be uploaded.

## Results

| Date | Premiere version | OS | Footage | A: mean / p99 | Capability test notes | Tester |
|---|---|---|---|---|---|---|
| | | | | | | |
