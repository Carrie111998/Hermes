# Audio-Reactive Reference

Patterns for driving visuals from audio — spectrum analysis, beat detection, envelope following.

## Audio Input

```python
# Live input from audio interface
audio_in = root.create(audiodeviceinCHOP, 'audio_in')
audio_in.par.rate = 44100

# OR: from audio file (for testing)
audio_file = root.create(audiofileinCHOP, 'audio_in')
audio_file.par.file = '/path/to/track.wav'
audio_file.par.play = True
audio_file.par.repeat = 'on'       # NOT par.loop
audio_file.par.playmode = 'locked'
```

---

## Audio Band Extraction (Verified TD 2025.32460)

Use `audiofilterCHOP` for band separation (NOT `selectCHOP` by channel index):

```python
# Audio input
af = root.create(audiofileinCHOP, 'audio_in')
af.par.file = path
af.par.play = True
af.par.repeat = 'on'
af.par.playmode = 'locked'

# Low band: lowpass @ 250Hz
flt_low = root.create(audiofilterCHOP, 'flt_low')
flt_low.par.filter = 'lowpass'
flt_low.par.cutofffrequency = 250
flt_low.par.rolloff = 2
flt_low.inputConnectors[0].connect(af)

# Mid band: highpass@250 → lowpass@4000
flt_mid_hp = root.create(audiofilterCHOP, 'flt_mid_hp')
flt_mid_hp.par.filter = 'highpass'
flt_mid_hp.par.cutofffrequency = 250
flt_mid_hp.par.rolloff = 2
flt_mid_hp.inputConnectors[0].connect(af)

flt_mid_lp = root.create(audiofilterCHOP, 'flt_mid_lp')
flt_mid_lp.par.filter = 'lowpass'
flt_mid_lp.par.cutofffrequency = 4000
flt_mid_lp.par.rolloff = 2
flt_mid_lp.inputConnectors[0].connect(flt_mid_hp)

# High band: highpass @ 4000Hz
flt_high = root.create(audiofilterCHOP, 'flt_high')
flt_high.par.filter = 'highpass'
flt_high.par.cutofffrequency = 4000
flt_high.par.rolloff = 2
flt_high.inputConnectors[0].connect(af)

# Per-band: RMS → lag → gain → clamp
for name, filt in [('low', flt_low), ('mid', flt_mid_lp), ('high', flt_high)]:
    rms = root.create(analyzeCHOP, f'rms_{name}')
    rms.par.function = 'rmspower'  # NOT 'rms'
    rms.inputConnectors[0].connect(filt)

    lag = root.create(lagCHOP, f'lag_{name}')
    lag.par.lag1 = 0.05   # attack (NOT par.lagin)
    lag.par.lag2 = 0.25   # release (NOT par.lagout)
    lag.inputConnectors[0].connect(rms)

    math = root.create(mathCHOP, f'scale_{name}')
    math.par.gain = 8.0
    math.inputConnectors[0].connect(lag)

    # mathCHOP has NO par.clamp — use limitCHOP
    lim = root.create(limitCHOP, f'clamp_{name}')
    lim.par.type = 'clamp'
    lim.par.min = 0.0
    lim.par.max = 1.0
    lim.inputConnectors[0].connect(math)

    null = root.create(nullCHOP, f'out_{name}')
    null.inputConnectors[0].connect(lim)
    null.viewer = True
```

**Key TD 2025 corrections:**
- `analyzeCHOP.par.function = 'rmspower'` NOT `'rms'`
- `lagCHOP.par.lag1` / `par.lag2` NOT `par.lagin` / `par.lagout`
- `mathCHOP` has NO `par.clamp` — use separate `limitCHOP`

---

## Beat / Onset Detection

### Kick Detection (slope → trigger)

```python
slope = root.create(slopeCHOP, 'kick_slope')
slope.inputConnectors[0].connect(op('out_low'))

trig = root.create(triggerCHOP, 'kick_trig')
trig.par.threshold = 0.12
trig.par.attack = 0.005    # NOT par.attacktime
trig.par.decay = 0.15       # NOT par.decaytime
trig.par.triggeron = 'increase'
trig.inputConnectors[0].connect(slope)

kick_out = root.create(nullCHOP, 'out_kick')
kick_out.inputConnectors[0].connect(trig)
```

---

## Passing Audio to GLSL

```python
glsl.par.vec0name = 'uLow'
glsl.par.vec0valuex.expr = "op('out_low')['chan1']"
glsl.par.vec0valuex.mode = ParMode.EXPRESSION

glsl.par.vec1name = 'uKick'
glsl.par.vec1valuex.expr = "op('out_kick')['chan1']"
glsl.par.vec1valuex.mode = ParMode.EXPRESSION
```

```glsl
uniform float uLow;
uniform float uKick;
float scale = 1.0 + uKick * 0.4 + uLow * 0.2;
```

---

## Standard Audio Bus Pattern

Recommended structure:

```
audiodeviceinCHOP (audio_in)
        ↓
  [null_audio_in]
        ├──→ audiofilterCHOP (lowpass@250) → analyzeCHOP → lagCHOP → mathCHOP → limitCHOP → null
        ├──→ audiofilterCHOP (bandpass@250-4k) → analyzeCHOP → lagCHOP → mathCHOP → limitCHOP → null
        ├──→ audiofilterCHOP (highpass@4k) → analyzeCHOP → lagCHOP → mathCHOP → limitCHOP → null
        │
        └──→ slopeCHOP → triggerCHOP (beat_trigger)
```

Keep this entire bus inside a `baseCOMP` (e.g., `audio_bus`) and reference via paths from visual networks.

---

## MIDI Input

```python
midi_in = root.create(midiinCHOP, 'midi_in')
midi_in.par.device = 0  # Check midiinDAT for device index
# Outputs channels named by MIDI note/CC: 'ch1n60', 'ch1c74', etc.

# Map CC to a parameter
op('bloom1').par.threshold.mode = ParMode.EXPRESSION
op('bloom1').par.threshold.expr = "op('midi_in')['ch1c74'][0]"
```

---

## CRITICAL: DO NOT use Lag CHOP for spectrum smoothing

Lag CHOP in timeslice mode expands 256-sample spectrum to 1600-2400 samples, averaging all values to near-zero (~1e-06). The shader receives no usable data. Use `mathCHOP(gain=8)` directly, or smooth in GLSL via temporal lerp with a feedback texture.

Verified:
- Without Lag CHOP: bass bins = 5.0-5.4 (strong, usable)
- With Lag CHOP: ALL bins = 0.000001 (dead)

---

## Spectrum → GLSL TOP Proven Recipe

Tested April 2026 against TD 2025.32. Use this chain when a shader needs a live
FFT spectrum texture rather than a few band scalars.

### Correct signal chain

```
AudioFileIn CHOP (playmode=sequential)
  → AudioSpectrum CHOP (FFT=512, outputmenu=setmanually, outlength=256, timeslice=ON)
  → Math CHOP (gain=10)
  → CHOP to TOP (dataformat=r, layout=rowscropped)
  → GLSL TOP input 1 (spectrum texture, 256x2)

Constant TOP (rgba32float, time) → GLSL TOP input 0
GLSL TOP → Null TOP → MovieFileOut
```

### Critical rules (empirically verified)

1. **TimeSlice must stay ON** for AudioSpectrum. OFF = processes entire audio file → 24000+ samples → CHOP to TOP overflow.
2. **Set Output Length manually** to 256 via `outputmenu='setmanually'` and `outlength=256`. Default outputs 22050 samples.
3. **DO NOT use Lag CHOP for spectrum smoothing.** Lag CHOP operates in timeslice mode and expands 256 samples to 2400+, averaging all values to near-zero (~1e-06). The shader receives no usable data. This was the #1 audio sync failure in testing.
4. **DO NOT use Filter CHOP either** — same timeslice expansion problem with spectrum data.
5. **Smoothing belongs in the GLSL shader** if needed, via temporal lerp with a feedback texture: `mix(prevValue, newValue, 0.3)`. This gives frame-perfect sync with zero pipeline latency.
6. **CHOP to TOP dataformat = 'r'**, layout = 'rowscropped'. Spectrum output is 256x2 (stereo). Sample at y=0.25 for first channel.
7. **Math gain = 10** (not 5). Raw spectrum values are ~0.19 in bass range. Gain of 10 gives usable ~5.0 for the shader.
8. **No Resample CHOP needed.** Control output size via AudioSpectrum's `outlength` param directly.

### GLSL spectrum sampling

```glsl
// Input 0 = time (1x1 rgba32float), Input 1 = spectrum (256x2)
float iTime = texture(sTD2DInputs[0], vec2(0.5)).r;

// Sample multiple points per band and average for stability:
// NOTE: y=0.25 for first channel (stereo texture is 256x2, first row center is 0.25)
float bass = (texture(sTD2DInputs[1], vec2(0.02, 0.25)).r +
              texture(sTD2DInputs[1], vec2(0.05, 0.25)).r) / 2.0;
float mid  = (texture(sTD2DInputs[1], vec2(0.2, 0.25)).r +
              texture(sTD2DInputs[1], vec2(0.35, 0.25)).r) / 2.0;
float hi   = (texture(sTD2DInputs[1], vec2(0.6, 0.25)).r +
              texture(sTD2DInputs[1], vec2(0.8, 0.25)).r) / 2.0;
```

See `references/network-patterns.md` for complete build scripts + shader code.
