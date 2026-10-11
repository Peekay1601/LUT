# Premiere integration capability matrix

Target: **Premiere Pro 25.6+ with UXP plugins** (built for 26.5 on macOS) (UXP is GA from 25.6.0; 25.2 was the public beta).
Sources: Adobe's UXP Premiere reference (`developer.adobe.com/premiere-pro/uxp/ppro-reference/`, repo
`AdobeDocs/uxp-premiere-pro`, checked 2026-10-11) and `AdobeDocs/uxp-premiere-pro-samples`.

Legend: **Documented** = in the reference or an official sample · **Verified** = observed working in a real
Premiere install · **Unsupported** = no documented API; not used.

Nothing below is marked *Verified* yet. This repository was built without access to Premiere. Run the
panel's **Run capability test** and [ACCEPTANCE.md](ACCEPTANCE.md) on your install, then update this table.

| Capability | API | Status | Used by the panel |
|---|---|---|---|
| Load as a panel | `manifest.json` v5, `host.app = "premierepro"`, `minVersion 25.6.0` | Documented | yes |
| Active project / sequence | `Project.getActiveProject()`, `project.getActiveSequence()` | Documented | yes |
| Read timeline selection | `sequence.getSelection()` → `TrackItemSelection.getTrackItems()` | Documented | yes |
| Clip source path | `ClipProjectItem.cast(projectItem).getMediaFilePath()` | Documented | yes |
| Source in/out vs timeline in/out | `getInPoint/getOutPoint` vs `getStartTime/getEndTime` (`TickTime.seconds`) | Documented; **whether in/out points are relative to media start or timecode needs verifying** | yes |
| Speed / reverse / disabled | `getSpeed()`, `isSpeedReversed()`, `isDisabled()` | Documented | yes (flags) |
| Nested sequence / offline | `ClipProjectItem.isSequence()`, `isOffline()` | Documented | yes (flags) |
| Existing source Input LUT | `ClipProjectItem.getInputLUTID()` | Documented | yes (flag) |
| Existing effects on a clip | `getComponentChain()`, `getComponentCount/AtIndex`, `getMatchName()` | Documented | yes (flags Lumetri) |
| Frame rate / timebase | `getSettings().getVideoFrameRate().value`, `getTimebase()` | Documented | yes |
| Multicam detection | — | **Not found**: multicam clips are not detected automatically | no |
| Clip colour-space override (Interpret Footage) | — | **Not found**: the manifest cannot report it | no |
| Lumetri available | `VideoFilterFactory.getMatchNames()` contains a Lumetri match name | Documented factory; the **Lumetri match name is not documented** (`AE.ADBE Lumetri` is an assumption that is checked at runtime) | capability test only |
| Add an effect | `VideoFilterFactory.createComponent()` + `chain.createAppendComponentAction()` inside `project.executeTransaction()` | Documented | **no** (MVP is non-destructive) |
| Set effect parameters | `ComponentParam.createKeyframe()` + `createSetValueAction()` | Documented generically; **Lumetri parameter indices/meanings are not documented** | read-only inspector only |
| Assign a LUT file to Lumetri (Input LUT / Look) | — | **Not documented.** The panel's *Apply to clip* (beta) tries it with documented calls: it appends a Lumetri Color, sets the parameter named Look/LUT to the .cube path with `createKeyframe` + `createSetValueAction` in one undoable transaction, reads it back, and removes that Lumetri again if it didn't stick | yes, beta, with manual fallback |
| Set a media Input LUT | `ClipProjectItem.createSetInputLUTIDAction(id)` | Documented, but **no documented way to get an ID for a .cube file** | no |
| Undo transactions | `project.executeTransaction(cb, undoString)`, `lockedAccess` | Documented | not needed yet |
| Export a rendered frame | `Exporter.exportSequenceFrame(seq, time, name, folder, w, h)` | Documented | not yet (see the next steps in the README) |
| Export the sequence | `EncoderManager.getManager().exportSequence(...)` | Documented | no |
| Markers / XMP metadata | `Markers.getMarkers()`, `Metadata.*` | Documented | no |
| `http://` from the panel | manifest `network.domains` | Documented: **Premiere disallows `http://` on macOS** | no → the panel and engine exchange job files instead |
| HTTPS to `api.anthropic.com` | `fetch` + `network.domains` | Documented | yes (Claude calls with your key) |
| Read/write `~/ConsistentGrade` | `localFileSystem: "fullAccess"`, `require("fs")`, `os.homedir()` | Documented | yes (job files) |
| Executable start script | `fs.writeFile(path, data, {mode: 0o755})` | Documented (`options.mode`) | yes |
| Open the script / report | `uxp.shell.openPath()` + `launchProcess.extensions` (consent dialog) | Documented | yes |
| Install by double-click | `.ccx` (ZIP, manifest at root) → Creative Cloud desktop | Documented | yes (`dist/ConsistentGrade.ccx`) |
| Secret storage | `uxp.storage.secureStorage` | Documented UXP module | yes (API key) |
| QE DOM / UI automation | — | Not used (undocumented; would need your approval) | no |

## Consequences for the MVP

1. The panel only **reads**. It never edits the sequence, so there is nothing to revert.
2. LUTs are applied **by hand** in Lumetri, with copy-path buttons and instructions in the panel.
3. Analysis grades **clean source media** decoded by FFmpeg. Effects, Lumetri, Interpret Footage overrides
   and input LUTs already applied in Premiere are not seen, so the panel flags clips that have them.
4. An automatic-apply path would need either a documented Lumetri LUT API in a future Premiere version,
   or a native C++ effect plug-in that reads `grade.json` (a much bigger project).
