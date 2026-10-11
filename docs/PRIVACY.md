# Where your footage goes

## Without Claude (the "Ask Claude" box unticked)

Nothing leaves your Mac.

* FFmpeg reads your original media files **read-only**. They are never modified, copied or uploaded.
* Sample frames, previews, LUTs and the report are written to `~/ConsistentGrade/jobs/<job>/` on
  your Mac. Decoded-frame caches go to `~/ConsistentGrade/cache/`. Delete either folder whenever you like.
* The panel makes no network requests in this mode. The engine only uses the network during
  first-time setup, to download Python packages and FFmpeg.

## With Claude (box ticked, using your API key)

Only these are sent, over HTTPS, directly from the panel to `api.anthropic.com`:

* **One still image of the reference**, at most 960 px wide.
* **One small contact strip per shot**: about 5 thumbnails, each 240 px wide.
* **Numbers**: exposure, colour and contrast measurements, plus the file names of your clips.

**No video, no audio, and no full-resolution frames are sent.** The request goes to the Anthropic API under
the account and organisation that owns the API key. It does not appear in anyone's Claude.ai chats.
Under Anthropic's commercial terms, API inputs are not used to train models by default. Anthropic keeps
API data only for a limited period under its data-retention policy, and organisations can arrange
zero data retention. Check your organisation's agreement and Anthropic's current Privacy Center for the
exact terms. People who administer that API key's organisation can see its usage and cost.

## Your API key

* Stored with UXP **secureStorage**: encrypted and tied to your macOS user account.
* Sent only in the HTTPS request to `api.anthropic.com`. It is never written to `~/ConsistentGrade`,
  the job files, logs or reports, and the engine never sees it.
* **Forget key** in the panel removes it.

If footage is confidential, leave **Ask Claude** unticked. Matching and LUT generation work fully offline.
