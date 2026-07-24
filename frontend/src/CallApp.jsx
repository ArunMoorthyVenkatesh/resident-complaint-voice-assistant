import { useState, useEffect, useRef, useCallback } from 'react';
import './styles.css';

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001';
const GEMINI_WS_URL = API_BASE_URL.replace(/^https?/, API_BASE_URL.startsWith('https') ? 'wss' : 'ws') + '/gemini-ws';
const API_KEY = import.meta.env.VITE_API_KEY;

const uid = () => 'session-' + Date.now() + '-' + Math.random().toString(36).slice(2, 9);

const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;

export default function App() {
  const [convActive,   setConvActive]   = useState(false);
  const [harrySpeaking,setHarrySpeaking]= useState(false);
  const [recording,    setRecording]    = useState(false);
  const [vError,       setVError]       = useState('');
  const [summary,      setSummary]      = useState(null);
  const [collected,    setCollected]    = useState({});
  const [callSecs,     setCallSecs]     = useState(0);
  const [clockTime,    setClockTime]    = useState(() =>
    new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  );

  const autoListenRef  = useRef(false);
  const endConvRef     = useRef(null);
  const micStreamRef   = useRef(null);
  const lastEndTimeRef = useRef(0);
  const timerRef       = useRef(null);

  const geminiWsRef    = useRef(null);
  const captureCtxRef  = useRef(null);
  const workletNodeRef = useRef(null);
  const sourceNodeRef  = useRef(null);
  const playCtxRef     = useRef(null);
  const nextPlayTimeRef= useRef(0);
  const activeSrcRef   = useRef(0);
  const activeSrcNodes = useRef(new Set());
  const awaitingHangupRef    = useRef(false);
  const mayaSpokeAfterSaveRef= useRef(false);
  const hangupFallbackRef    = useRef(null);
  const hangupPollRef        = useRef(null);

  useEffect(() => () => {
    autoListenRef.current = false;
    clearInterval(timerRef.current);
    try { geminiWsRef.current?.close(); } catch (_) {}
    workletNodeRef.current?.disconnect();
    sourceNodeRef.current?.disconnect();
    captureCtxRef.current?.close().catch(() => {});
    playCtxRef.current?.close().catch(() => {});
    micStreamRef.current?.getTracks().forEach(t => t.stop());
  }, []);

  useEffect(() => {
    const id = setInterval(() =>
      setClockTime(new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))
    , 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (convActive) {
      setCallSecs(0);
      timerRef.current = setInterval(() => setCallSecs(s => s + 1), 1000);
    } else {
      clearInterval(timerRef.current);
    }
    return () => clearInterval(timerRef.current);
  }, [convActive]);

  const stopAllAudio = useCallback(() => {
    activeSrcNodes.current.forEach(src => { try { src.stop(); } catch (_) {} });
    activeSrcNodes.current.clear();
    activeSrcRef.current = 0;
    nextPlayTimeRef.current = playCtxRef.current?.currentTime ?? 0;
    setHarrySpeaking(false);
  }, []);

  const playPCMChunk = useCallback((arrayBuffer) => {
    const ctx = playCtxRef.current;
    if (!ctx || !autoListenRef.current) return;
    setHarrySpeaking(true);
    const int16 = new Int16Array(arrayBuffer);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
    const buf = ctx.createBuffer(1, float32.length, 24000);
    buf.copyToChannel(float32, 0);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    const startAt = Math.max(nextPlayTimeRef.current, ctx.currentTime + 0.01);
    src.start(startAt);
    nextPlayTimeRef.current = startAt + buf.duration;
    activeSrcRef.current++;
    activeSrcNodes.current.add(src);
    src.onended = () => {
      activeSrcNodes.current.delete(src);
      activeSrcRef.current = Math.max(0, activeSrcRef.current - 1);
      if (activeSrcRef.current === 0) setHarrySpeaking(false);
    };
  }, []);

  const startConversation = useCallback(async () => {
    setConvActive(true);
    autoListenRef.current = true;
    awaitingHangupRef.current = false;
    mayaSpokeAfterSaveRef.current = false;
    clearTimeout(hangupFallbackRef.current);
    setVError('');
    setCollected({});
    setSummary(null);
    setHarrySpeaking(false);
    setRecording(false);

    const msSinceLast = Date.now() - lastEndTimeRef.current;
    if (msSinceLast < 800) await new Promise(r => setTimeout(r, 800 - msSinceLast));

    // Start playback context
    const playCtx = new AudioContext();
    playCtx.resume().catch(() => {});
    playCtxRef.current = playCtx;
    nextPlayTimeRef.current = playCtx.currentTime;
    activeSrcRef.current = 0;

    // Open WebSocket immediately — this kicks off Gemini session init on the backend
    // while mic permission + AudioWorklet load happen in parallel below
    const ws = new WebSocket(`${GEMINI_WS_URL}?api_key=${API_KEY}`);
    ws.binaryType = 'arraybuffer';
    geminiWsRef.current = ws;

    const wsReady = new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', () => reject(new Error('ws')), { once: true });
    });

    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        playPCMChunk(event.data);
      } else {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'interrupted') {
            stopAllAudio();
          } else if (msg.type === 'complaint_saved') {
            setCollected(msg.complaint_data || {});
            setSummary({ ...msg.complaint_data, complaint_id: msg.complaint_id });
            awaitingHangupRef.current = true;
            mayaSpokeAfterSaveRef.current = activeSrcRef.current > 0;
            clearTimeout(hangupFallbackRef.current);
            clearTimeout(hangupPollRef.current);
            // Hard fallback in case playback state is never observed correctly.
            hangupFallbackRef.current = setTimeout(() => {
              if (awaitingHangupRef.current) { awaitingHangupRef.current = false; endConvRef.current?.(); }
            }, 15000);
            // Poll the actual audio-source count directly rather than reacting
            // to harrySpeaking state transitions -- Web Audio scheduling can
            // produce brief gaps between chunks that flicker the state false
            // mid-reply, which would falsely look like "she's done talking".
            const poll = () => {
              if (!awaitingHangupRef.current) return;
              if (activeSrcRef.current > 0) {
                mayaSpokeAfterSaveRef.current = true;
              } else if (mayaSpokeAfterSaveRef.current) {
                awaitingHangupRef.current = false;
                clearTimeout(hangupFallbackRef.current);
                hangupPollRef.current = setTimeout(() => endConvRef.current?.(), 800);
                return;
              }
              hangupPollRef.current = setTimeout(poll, 250);
            };
            hangupPollRef.current = setTimeout(poll, 250);
          } else if (msg.type === 'error') {
            setVError(msg.message || 'Connection error.');
          }
        } catch (_) {}
      }
    };
    ws.onerror = () => setVError('Connection error. Please try again.');
    ws.onclose = () => {
      if (autoListenRef.current) {
        setConvActive(false);
        autoListenRef.current = false;
        setRecording(false);
        setHarrySpeaking(false);
      }
    };

    // Mic permission + AudioWorklet load in parallel with WebSocket connecting
    let stream, captureCtx;
    try {
      captureCtx = new AudioContext({ sampleRate: 16000 });
      captureCtxRef.current = captureCtx;
      [stream] = await Promise.all([
        navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        }),
        captureCtx.audioWorklet.addModule('/gemini-audio-processor.js'),
        wsReady,
      ]);
      micStreamRef.current = stream;
    } catch (err) {
      const msg = err?.name === 'NotAllowedError' || err?.message === 'ws'
        ? err.message === 'ws' ? 'Connection error. Please try again.' : 'Microphone access denied.'
        : 'Audio setup failed. Please refresh and try again.';
      setVError(msg);
      setConvActive(false);
      autoListenRef.current = false;
      try { ws.close(); } catch (_) {}
      captureCtx?.close().catch(() => {});
      playCtx.close();
      return;
    }

    // All three ready — connect mic to WebSocket
    const source  = captureCtx.createMediaStreamSource(stream);
    const worklet = new AudioWorkletNode(captureCtx, 'gemini-audio-processor');
    worklet.port.onmessage = ({ data }) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(data);
    };
    source.connect(worklet);
    sourceNodeRef.current = source;
    workletNodeRef.current = worklet;
    setRecording(true);
  }, [playPCMChunk, stopAllAudio]);

  const endConversation = useCallback(() => {
    autoListenRef.current = false;
    awaitingHangupRef.current = false;
    mayaSpokeAfterSaveRef.current = false;
    clearTimeout(hangupFallbackRef.current);
    clearTimeout(hangupPollRef.current);
    stopAllAudio();
    try { geminiWsRef.current?.close(); } catch (_) {}
    geminiWsRef.current = null;
    try { workletNodeRef.current?.port.postMessage('stop'); } catch (_) {}
    workletNodeRef.current?.disconnect();
    sourceNodeRef.current?.disconnect();
    workletNodeRef.current = null;
    sourceNodeRef.current  = null;
    captureCtxRef.current?.close().catch(() => {});
    captureCtxRef.current = null;
    playCtxRef.current?.close().catch(() => {});
    playCtxRef.current    = null;
    nextPlayTimeRef.current = 0;
    activeSrcRef.current    = 0;
    micStreamRef.current?.getTracks().forEach(t => t.stop());
    micStreamRef.current = null;
    setConvActive(false);
    setRecording(false);
    setHarrySpeaking(false);
    setVError('');
    lastEndTimeRef.current = Date.now();
  }, [stopAllAudio]);

  endConvRef.current = endConversation;

  const hasCollected = collected.description || collected.location;

  return (
    <div className="call-screen">

      {/* Status bar */}
      <div className="cs-statusbar">
        <span className="cs-clock">{clockTime}</span>
      </div>

      {/* Caller identity */}
      <div className="cs-identity">
        <div className="cs-company">STE BuildCare</div>
        <div className="cs-name">Maya</div>
        <div className="cs-subtitle">Building Complaint Assistant</div>
        {convActive && <div className="cs-timer">{fmt(callSecs)}</div>}
        {!convActive && !summary && <div className="cs-idle-hint">Tap to start</div>}
        {!convActive && summary && (
          <div className="cs-saved-pill">✓ Logged · {summary.complaint_id}</div>
        )}
      </div>

      {/* Live complaint info — appears as data is collected */}
      {convActive && hasCollected && (
        <div className="cs-info-card">
          {collected.description && (
            <div className="cs-info-row">
              <span className="cs-info-label">Issue</span>
              <span className="cs-info-val">{collected.description}</span>
            </div>
          )}
          {collected.location && (
            <div className="cs-info-row">
              <span className="cs-info-label">Location</span>
              <span className="cs-info-val">{collected.location}</span>
            </div>
          )}
          {collected.name && (
            <div className="cs-info-row">
              <span className="cs-info-label">Name</span>
              <span className="cs-info-val">{collected.name}</span>
            </div>
          )}
        </div>
      )}

      {/* Waveform */}
      {convActive && (
        <div className={`cs-wave ${harrySpeaking ? 'cs-wave--maya' : recording ? 'cs-wave--user' : ''}`}>
          {[...Array(28)].map((_, i) => (
            <span key={i} className="cs-wave-bar" style={{ '--i': i }} />
          ))}
        </div>
      )}

      {/* Error */}
      {vError && <div className="cs-error">{vError}</div>}

      {/* Controls */}
      <div className="cs-controls">
        {convActive ? (
          <>
            <button className="cs-util-btn" disabled title="Mute">
              <svg viewBox="0 0 24 24"><line x1="1" y1="1" x2="23" y2="23"/><path d="M9 9v3a3 3 0 005.12 2.12M15 9.34V4a3 3 0 00-5.94-.6"/><path d="M17 16.95A7 7 0 015 12v-2m14 0v2a7 7 0 01-.11 1.23"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
              <span>Mute</span>
            </button>
            <button className="cs-end-btn" onClick={endConversation}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M10.68 13.31a16 16 0 003.41 2.6l1.27-1.27a2 2 0 012.11-.45c1.12.44 2.33.68 3.53.68a2 2 0 012 2v3.5a2 2 0 01-2 2A18 18 0 012 4a2 2 0 012-2H7.5a2 2 0 012 2c0 1.25.25 2.45.68 3.57a2 2 0 01-.45 2.11L8.4 10.9a16 16 0 002.28 2.41z"/>
              </svg>
            </button>
            <button className="cs-util-btn" disabled title="Speaker">
              <svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 010 14.14M15.54 8.46a5 5 0 010 7.07"/></svg>
              <span>Speaker</span>
            </button>
          </>
        ) : (
          <button className="cs-call-btn" onClick={startConversation}>
            <svg viewBox="0 0 24 24"><path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07A19.5 19.5 0 013.89 11a19.79 19.79 0 01-3.07-8.67A2 2 0 012.8 0h3a2 2 0 012 1.72 12.84 12.84 0 00.7 2.81 2 2 0 01-.45 2.11L7.09 7.91a16 16 0 006 6l.91-.91a2 2 0 012.11-.45 12.84 12.84 0 002.81.7A2 2 0 0122 15.42z"/></svg>
          </button>
        )}
      </div>

    </div>
  );
}
