class HermesVoicePlayoutProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this.maxSamples = options?.processorOptions?.maxBufferSamples || 24000
    this.queue = []
    this.queuedSamples = 0
    this.drainRequest = null
    this.cancelled = false
    this.started = false
    this.stableCallbacks = 0
    this.underrunActive = false
    this.drainReady = false
    this.port.onmessage = event => {
      const message = event.data || {}

      if (message.type === 'start') {
        this.started = true
      } else if (message.type === 'pause') {
        this.started = false
      } else if (message.type === 'write' && message.samples) {
        const samples = new Float32Array(message.samples)

        if (this.queuedSamples + samples.length > this.maxSamples) {
          this.port.postMessage({ type: 'overflow' })
          return
        }

        this.queue.push(samples)
        this.queuedSamples += samples.length
      } else if (message.type === 'drain') {
        this.drainRequest = message.id
      } else if (message.type === 'cancel') {
        this.queue = []
        this.queuedSamples = 0
        this.cancelled = true
      }
    }
  }

  process(_inputs, outputs) {
    const output = outputs[0]
    const channel = output && output[0]

    if (!channel) {
      return !this.cancelled
    }

    // A terminal drain may arrive after an underrun paused the clock.  There
    // is no audio left to render in that state, so acknowledge the request
    // even though normal playback is paused; otherwise the client waits for
    // a `drained` message that can never be produced.
    if (!this.started && this.drainRequest !== null && this.queuedSamples === 0) {
      channel.fill(0)
      this.port.postMessage({ id: this.drainRequest, type: 'drained' })
      this.drainRequest = null
      this.drainReady = false
      return !this.cancelled
    }

    if (!this.started) {
      channel.fill(0)
      return !this.cancelled
    }

    if (this.drainReady) {
      channel.fill(0)
      this.port.postMessage({ id: this.drainRequest, type: 'drained' })
      this.drainRequest = null
      this.drainReady = false
      return !this.cancelled
    }

    let written = 0

    while (written < channel.length && this.queue.length > 0) {
      const current = this.queue[0]
      const count = Math.min(channel.length - written, current.length)
      channel.set(current.subarray(0, count), written)
      written += count
      this.queuedSamples -= count

      if (count === current.length) {
        this.queue.shift()
      } else {
        this.queue[0] = current.subarray(count)
      }
    }

    if (written < channel.length) {
      channel.fill(0, written)
      this.stableCallbacks = 0
      if (!this.cancelled && this.drainRequest === null && !this.underrunActive) {
        this.underrunActive = true
        this.port.postMessage({ type: 'underrun' })
      }
    } else {
      this.underrunActive = false
      this.stableCallbacks += 1
      if (this.stableCallbacks >= 25) {
        this.stableCallbacks = 0
        this.port.postMessage({ type: 'stable' })
      }
    }

    if (this.drainRequest !== null && this.queuedSamples === 0) {
      this.drainReady = true
    }

    return !this.cancelled
  }
}

registerProcessor('hermes-voice-playout', HermesVoicePlayoutProcessor)

// Exporting the processor keeps the worklet's behavior directly testable in
// the renderer suite; AudioWorklet ignores module exports at runtime.
export { HermesVoicePlayoutProcessor }
