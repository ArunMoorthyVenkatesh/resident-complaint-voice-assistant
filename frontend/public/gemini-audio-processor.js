/**
 * gemini-audio-processor.js — AudioWorklet processor for Gemini Live mic capture.
 *
 * Runs in the audio rendering thread. Accumulates Float32 samples from the mic
 * into 100ms chunks (1600 samples at 16kHz), converts them to Int16, and posts
 * the buffer to the main thread for sending over WebSocket.
 *
 * Loaded via: captureCtx.audioWorklet.addModule('/gemini-audio-processor.js')
 * Used as:    new AudioWorkletNode(captureCtx, 'gemini-audio-processor')
 */
class GeminiAudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunkSamples = 512; // 32ms at 16kHz — smaller chunks = faster end-of-speech detection
    this._buf = new Float32Array(this._chunkSamples);
    this._pos = 0;
    this._active = true;
    this.port.onmessage = (e) => {
      if (e.data === 'stop') this._active = false;
    };
  }

  process(inputs) {
    if (!this._active) return false;
    const ch = inputs[0]?.[0];
    if (!ch) return true;

    for (let i = 0; i < ch.length; i++) {
      this._buf[this._pos++] = ch[i];

      if (this._pos >= this._chunkSamples) {
        // Convert Float32 [-1, 1] → Int16 [-32768, 32767]
        const int16 = new Int16Array(this._chunkSamples);
        for (let j = 0; j < this._chunkSamples; j++) {
          int16[j] = Math.max(-32768, Math.min(32767, Math.round(this._buf[j] * 32767)));
        }
        // Transfer ownership — zero-copy
        this.port.postMessage(int16.buffer, [int16.buffer]);
        this._pos = 0;
      }
    }
    return true;
  }
}

registerProcessor('gemini-audio-processor', GeminiAudioProcessor);
