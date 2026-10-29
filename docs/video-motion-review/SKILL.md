---
name: video-motion-review
description: Review AI-generated short video clips (Seedance-style, <=30 s, 24 fps, single take) with motion-aware frame extraction. Use when a clip must be judged before spending another generation, when comparing several takes of the same shot for regressions, when deciding whether a clip freezes, stalls, or breaks continuity, or when a clip must be turned into frames a vision model can actually read.
---

# Video Motion Review

Turn a generated clip into something a vision model can judge, and decide what to
look at *before* spending another generation.

## The framing that matters

A generated clip is **one continuous shot**. Two consequences drive everything here:

1. **ffmpeg scene detection is useless.** `select='gt(scene,..)'` sees a single
   scene and returns almost nothing. Do not use it on generated clips.
2. **What carries information is the motion-energy curve** -- where the clip moves,
   where it stalls, where it freezes. That is measurable with plain ffmpeg,
   in about 0.2 s for a 15 s clip.

Motion is a **triage signal, not a verdict**. It reliably finds *where* to look and
cannot judge *whether the content is right*. Never auto-reject on a flag.

## Budget arithmetic (fixed envelope)

For `<=30 s @ 24 fps` there are at most 720 frames, and the DSH message limit is
**20 images**, so:

| Frames | Coverage per image | Use |
|---|---|---|
| 16 (4x4) | ~1.9 s | whole-clip pass/fail |
| 20 (5x4) | ~1.5 s | whole-clip pass/fail, preferred |

Whole-clip evaluation is a **solved, bounded** problem -- one request, one sheet.
Do not chase higher fps to "cover" a clip; 24 fps consecutive frames are nearly
identical and 90% of a dense sample is redundant.

## Workflow

### 1. Scan first (free gate, ~0.2 s)

```powershell
& 'D:\AIGC\ComfyUI-aki-v2\tools\vscan.ps1' -Path clip.mp4 -ScanOnly
```

Prints a per-sample motion timeline with flags and an `ACTION:` list of timecodes.
Cost is negligible against one generation, so **run it on every take**.

### 2. Export the frames to actually look at

```powershell
& 'D:\AIGC\ComfyUI-aki-v2\tools\vscan.ps1' -Path clip.mp4 -Keep 20 -Spread -Sheet
```

`-Spread` splits the budget: motion-ranked picks plus even time coverage, so one
burst cannot consume the whole budget. Every exported frame carries a burned-in
timecode, so a flagged second maps straight to a tile in the sheet. `-Sheet`
builds a contact sheet ordered in time, left-to-right, top-to-bottom, with its
long edge kept under 2048 px so DSH does not rescale it.

Then emit the sheet inline with an ```img fence and read it.

### 3. Inspect flagged windows densely only when needed

```powershell
& 'D:\AIGC\ComfyUI-aki-v2\tools\vscan.ps1' -Path clip.mp4 -Segment 12,15 -Rate 4
```

### 4. Comparing takes of the same shot (highest-value use)

Rerolling a shot is the expensive action, so "which take do I keep" gets a fixed
output. `vcompare.ps1` scans every take, groups them by filename stem
(a trailing `-vN` is stripped), ranks them, and names the winner and the one to avoid.

```powershell
# one shot, four rerolls
& 'D:\AIGC\ComfyUI-aki-v2\tools\vcompare.ps1' -Dir 'D:\clips' -Filter 'shot*.mp4'

# explicit takes; -Sheet also renders the winner of each group to a sheet
& 'D:\AIGC\ComfyUI-aki-v2\tools\vcompare.ps1' -Path a.mp4,b.mp4,c.mp4 -Sheet -OutDir 'D:\review'
```

Rank order: frozen tail (worst), then freeze / stall / burst counts, then
interior-curve stability, then total flags. A static end card is **not** penalised
-- it is expected in this pipeline.

```
rank ver        dur flag    frz   stl   brs  tail.min    stbl     verdict
1    v5      15.10s    2      0     0     1      6.06   0.824 clean
2    v2      15.10s    5      1     2     2      3.84   0.798 freeze event
3    v3      15.10s    4      2     1     1      2.96   0.703 freeze event
4    v4      15.10s    8      2     3     3      2.62   0.709 FROZEN TAIL
keep    : sz-ep2-s4-notoken-v5  (clean, flagged=2, stability=0.824)
avoid   : sz-ep2-s4-notoken-v4  (frozen tail: last-second slope 0.003, span 0.19)
```

22 takes across 13 shots scan in ~4.4 s total. Treat the table as a shortlist and
the winner's sheet as the actual decision.

## Calibrated thresholds

Derived from measured 4 s / 15 s / 30 s production clips:

- `FREEZE` -- motion `<= max(1.0, 0.15 * avg)`. A hard freeze reads ~1.3-1.5 at
  6 fps; a legitimately static tail still reads 2-8. Below 1.0 means identical frames.
- `stall` -- below `0.3 * local mean` (window +/- 2 s). Suppressed under 12 samples:
  a local mean from too few points produces false positives.
- `burst` -- `>= 0.9 * peak`.
- `TAIL-COLLAPSE` -- final ~2 s averaging below `0.35 * avg`.

## Traps that have already bitten

- **A near-static tail is usually an end card, not a render failure.** Generated
  clips often finish on a UI popup, overlay, or text card. A static end card
  legitimately reads near-zero motion. Flag it, then *look*.
- **A frozen tail is a real defect and looks different.** Compare the final
  second at 6 fps: a truly frozen version flatlines with no variation, while a
  still-animating UI card ramps or drifts.

  ```
  v4  final 1.2 s @6fps:  1.5 1.3 1.3 1.3 1.4 1.4   <- frozen
  v5  final 1.2 s @6fps:  2.5 2.9 3.7 4.3 4.6 4.0   <- still animating
  ```

- **Text in the image is the model's job, not the metric's.** Motion scanning
  cannot tell you that an end card says the wrong number or a character's face
  changed. That is exactly why step 2 exists -- the metric picks the frames, the
  vision model reads them.
- **Do not use JPEG byte histograms as a difference metric.** JPEG DCT byte order
  is not spatially meaningful. Use `tblend=all_mode=difference` + `signalstats`.
- **PowerShell specifics:** a parameter named `-Input` collides with the automatic
  `$input` variable and silently binds as an empty string -- these scripts use
  `-Path`/`-Video` for that reason. Keep `.ps1` files ASCII-only; PS 5.1
  misparses UTF-8 without a BOM.

## Metric reference

Frame difference in pure ffmpeg, no pixels leaving the process:

```
fps=<R>,scale=320:-2,format=gray,tblend=all_mode=difference,signalstats,metadata=print:file=-
```

`tblend=difference` subtracts each frame from its predecessor; `signalstats` then
reports `YAVG`, the mean absolute per-pixel change. Read `YAVG` per frame from the
`metadata=print` output. Runs at ~40-75x realtime.

## Tooling

- `tools/vscan.ps1` (in the workspace) -- motion scan, anomaly flags, ranked
  selection, timecoded export, contact sheet. Primary tool.
  Add `-Json -ScanOnly` for a single machine-readable scan object
  (`flagged`, `freezeCount`, `stallCount`, `burstCount`, `tail.frozen`,
  `tail.secSlope`, `interiorSd`, `interiorAvg`, ...).
- `tools/vcompare.ps1` -- batch regression compare across takes of one shot.
  Wraps `vscan.ps1 -Json`, groups by stem, ranks, names keep/avoid.
- `tools/vprobe.ps1` -- metadata probe, even/shot-change/segment sampling.
  Kept for non-generated footage; its `-Scene` mode does not work on generated clips.
- All accept `-Ffmpeg` / `-Ffprobe` to override the hardcoded `D:\ffmpeg\bin` path.

### Limits of the compare table

- It measures **motion only**. It cannot see wrong text, a changed face,
  composition drift, or whether the take matches the prompt. A low anomaly count
  is a shortlist, not approval -- always render the winner and look at it.
- `stbl` (interior sd/avg) is meaningful **between takes of the same shot**,
  where motion levels are comparable. Do not compare `stbl` across different shots.
- `stall` is a raw count, so a longer take accrues more of them; when durations
  differ, prefer `tail.frozen` and per-second reasoning over raw counts.
- `freezeCount` depends on a per-clip `0.15 * avg` threshold, so a clip whose
  overall motion is very low shifts that floor. Trust `tail.frozen` (absolute,
  slope-based) over `freezeCount` for the definitive defect call.

## References

- Motion-curve selection and adaptive temporal zoom: [LENS](https://github.com/zhangce01/LENS),
  [T*](https://github.com/mll-lab-nu/TStar) -- designed for long-form search, so
  their sampling machinery is unnecessary at <=30 s, but the adaptive-zoom idea
  is the right instinct.
- Scene-deduplicated extraction + transcript for LLM review: [claude-real-video](https://github.com/HUANGCHIHHUNGLeo/claude-real-video)
  (MIT). Its transcript half is the useful part here, for clips with dialogue audio.
