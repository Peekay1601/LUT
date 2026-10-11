# Manual Premiere acceptance test

End-to-end parity with Premiere is **not yet verified**. Run this once per Premiere version and footage
type before trusting the LUTs, and record the result at the bottom.

## A. LUT matches the local preview

1. Run `python -m tests.fixtures ./footage`, then:
   `python -m autograde analyze --input ./footage --reference a_reference.mp4 --output ./results`
   (or run the same clips through the panel and use the job folder in `~/ConsistentGrade/jobs/`)
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

## B. Panel workflow (macOS)

1. Double-click `dist/ConsistentGrade.ccx` and confirm the install in Creative Cloud. Open
   Window › UXP Plugins › Consistent Grade.
2. **Start engine**: allow the prompt. Terminal opens and runs the first-time setup, and the panel then
   shows "Engine: running".
3. Paste your Anthropic key → Test key ("Key works") → Save key. Restart Premiere and confirm the key is
   still saved.
4. Select 3–5 clips → **Read selected clips**. Check that the source in/out values are source-media
   seconds: trim a clip's head and confirm the source start changes while its timeline position doesn't.
5. Speed-change one clip, nest one, and add Lumetri to one. All three should be flagged and skipped.
6. **Run capability test** and copy the table into Results below.
7. Analyse with Ask Claude off, check the previews, apply one LUT by hand (test A), and tick Approved.
   `approvals.json` should appear in `~/ConsistentGrade/jobs/<job>/`.
8. Repeat with Ask Claude on. The log should show "asking Claude (N images)" and the suggested look.

## Results

| Date | Premiere version | OS | Footage | A: mean / p99 | Capability test notes | Tester |
|---|---|---|---|---|---|---|
| | | | | | | |
