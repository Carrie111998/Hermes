# Recording and Exporting Video

## MovieFileOut recipe

```python
# via td_execute_python:
root = op('/project1')
rec = root.create(moviefileoutTOP, 'recorder')
op('/project1/out').outputConnectors[0].connect(rec.inputConnectors[0])
rec.par.type = 'movie'
rec.par.file = '/tmp/output.mov'
rec.par.videocodec = 'prores'  # Apple ProRes — NOT license-restricted on macOS
rec.par.record = True   # start
# rec.par.record = False  # stop (call separately later)
```

H.264/H.265/AV1 need a Commercial license. Use `prores` on macOS or `mjpa` as fallback.

Extract frames: `ffmpeg -i /tmp/output.mov -vframes 120 /tmp/frames/frame_%06d.png`

**TOP.save() is useless for animation** — it captures the same GPU texture every
time. Always use MovieFileOut.

## Before Recording: Checklist

1. **Verify FPS > 0** via `td_get_perf`. If FPS=0 the recording will be empty. See `references/pitfalls.md` #38-39.
2. **Verify shader output is not black** via `td_get_screenshot`. Black output = shader error or missing input. See `references/pitfalls.md` #8, #40.
3. **If recording with audio:** cue audio to start first, then delay recording by 3 frames. See `references/pitfalls.md` #19.
4. **Set output path before starting record** — setting both in the same script can race.
