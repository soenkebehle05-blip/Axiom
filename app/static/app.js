// Browser side of the voice loop: Web Speech API (STT) -> WebSocket -> streamed text + PCM audio.
(() => {
  const $ = (id) => document.getElementById(id);
  const transcript = $("transcript"), mic = $("mic"), interim = $("interim");
  const state = { ws: null, listening: false, speaking: false, sampleRate: 24000, serverTTS: true,
                  speechEndAt: 0, audioRole: "reply",
                  botEl: null, botText: "", lastPartial: "", partialTimer: null };

  // ---------- turn timing (console only) ----------
  // No timings are shown in the UI; one debug line per turn is logged for your own measurements.
  const timing = { t0: 0, firstSound: 0, firstReply: 0, tool: false };
  const resetTiming = (t0) => { timing.t0 = t0; timing.firstSound = 0; timing.firstReply = 0; timing.tool = false; };
  const markSound = (at) => { if (!timing.firstSound) timing.firstSound = at; };
  const markReply = (at) => { if (!timing.firstReply) timing.firstReply = at; };
  const logTurnTiming = () => {
    if (!timing.t0) return;
    const ms = (t) => (t ? Math.round(t - timing.t0) + " ms" : "–");
    console.debug(`[turn] ${timing.tool ? "tool" : "plain"} · end of speech → first sound ${ms(timing.firstSound)} · → reply audio ${ms(timing.firstReply)}`);
    timing.t0 = 0;
  };

  // ---------- transcript helpers ----------
  const addMsg = (cls, text) => { const el = document.createElement("div"); el.className = "msg " + cls; el.textContent = text; transcript.appendChild(el); transcript.scrollTop = transcript.scrollHeight; return el; };
  const addTool = (name, args) => {
    const el = document.createElement("details"); el.className = "tool";
    const summary = document.createElement("summary"), details = document.createElement("pre");
    summary.textContent = `${name}(${Object.keys(args).join(", ")})`;
    details.textContent = JSON.stringify(args, null, 1);
    el.append(summary, details);
    transcript.appendChild(el); transcript.scrollTop = transcript.scrollHeight; return el;
  };

  // ---------- audio playback (PCM 16-bit -> Web Audio) ----------
  let ctx = null, nextTime = 0, sources = [];
  const ensureCtx = () => { if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: state.sampleRate }); if (ctx.state === "suspended") ctx.resume(); return ctx; };
  const playPCM = (buf) => {
    const ac = ensureCtx();
    if (buf.byteLength % 2) buf = buf.slice(0, buf.byteLength - 1);  // defensive: Int16Array needs an even byte count
    const i16 = new Int16Array(buf); const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    const audio = ac.createBuffer(1, f32.length, state.sampleRate); audio.copyToChannel(f32, 0);
    const src = ac.createBufferSource(); src.buffer = audio; src.connect(ac.destination);
    const startAt = Math.max(ac.currentTime + 0.02, nextTime); src.start(startAt); nextTime = startAt + audio.duration;
    sources.push(src); src.onended = () => { sources = sources.filter((s) => s !== src); if (!sources.length && state.audioDone) onSpeechDone(); };
    return performance.now() + (startAt - ac.currentTime) * 1000;  // when this chunk will actually be heard
  };
  const stopAudio = () => { sources.forEach((s) => { s.onended = null; try { s.stop(); } catch (_) {} }); sources = []; nextTime = 0; window.speechSynthesis?.cancel(); };
  // Runs once per turn (it can be triggered both by the last audio chunk ending and by the server's audio_end).
  const onSpeechDone = () => {
    state.speaking = false; mic.classList.remove("speaking");
    if (state.turnFinished) return;
    state.turnFinished = true; logTurnTiming();
    if (state.listenAfter === false) { stopListening(); return; }  // a booking was made: the goal is reached, keep the mic closed
    if ($("autolisten").checked && !state.listening) startListening();
  };

  // Browser TTS fallback (used when the server has no Cloud TTS credentials).
  const speakBrowser = (text, isAck = false) => {
    if (!text || !window.speechSynthesis) { if (!isAck) onSpeechDone(); return; }
    const u = new SpeechSynthesisUtterance(text); u.rate = 1.05;
    u.onstart = () => { const now = performance.now(); markSound(now); if (!isAck) markReply(now); };
    if (!isAck) u.onend = onSpeechDone;
    state.speaking = true; window.speechSynthesis.speak(u);
  };

  // ---------- calendar connection (Google sign-in, per browser) ----------
  const cal = { connected: false, source: "none", oauth: false };
  const calPanel = $("calpanel"), calStatus = $("calstatus");
  const setInputsEnabled = (on) => { mic.disabled = !on || (state.stt !== "deepgram" && !rec); $("textin").disabled = !on; };
  const showStatus = (text, cls = "") => { calStatus.textContent = text; calStatus.className = "hint status " + cls; };
  const openCalPanel = () => {
    $("calpanel-title").textContent = cal.source === "you" ? "Your Google Calendar" : cal.connected ? "Use your own Google Calendar" : "Connect a Google Calendar";
    $("calsignin").textContent = cal.source === "you" ? "Switch to another Google account" : "Sign in with Google";
    $("calsignin").hidden = !cal.oauth; $("caldisconnect").hidden = cal.source !== "you"; $("calclose").hidden = !cal.connected;
    showStatus(cal.oauth ? "" : "Google sign-in is not configured on this server; upload a token below instead.");
    calPanel.hidden = false;
  };
  const renderCalStatus = (s) => {
    cal.connected = !!s.connected; cal.source = s.source; cal.oauth = !!s.oauth_available;
    const who = s.calendar && (s.calendar.summary || s.calendar.id);
    const label = !cal.connected ? "Calendar: not connected – click to connect"
                : cal.source === "you" ? "Calendar: " + who + " (yours)" : "Calendar: " + (who || "default") + " – click to use your own";
    $("calmode").textContent = label; $("calmode").className = "pill clickable " + (cal.connected ? "ok" : "warn");
    $("calpanel-current").hidden = !cal.connected;
    if (cal.connected) $("calpanel-current").textContent = cal.source === "you" ? "Connected: " + who + ". Only this browser uses it." : "Currently using the demo's default calendar" + (who ? " (" + who + ")" : "") + ".";
    setInputsEnabled(cal.connected);
    if (!cal.connected) openCalPanel();
  };
  const refreshCalStatus = async () => { try { renderCalStatus(await (await fetch("/api/calendar/status")).json()); } catch (_) {} };
  const sendHello = () => { if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify({ type: "hello", timezone: Intl.DateTimeFormat().resolvedOptions().timeZone })); };
  const afterConnect = (data) => { renderCalStatus(data); showStatus("Connected to " + (data.calendar.summary || data.calendar.id) + ".", "ok"); setTimeout(() => { calPanel.hidden = true; }, 900); sendHello(); };
  $("calmode").addEventListener("click", openCalPanel);
  $("calclose").addEventListener("click", () => { calPanel.hidden = true; });
  $("calsignin").addEventListener("click", () => { location.href = "/api/calendar/oauth/start"; });
  $("caldisconnect").addEventListener("click", async () => {
    try { const res = await fetch("/api/calendar/token", { method: "DELETE" }); const data = await res.json(); renderCalStatus(data); showStatus(data.connected ? "Back to the default calendar." : "Disconnected.", "ok"); sendHello(); }
    catch (err) { showStatus("Could not disconnect: " + err.message, "err"); }
  });
  $("calform").addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = $("calfile").files[0]; if (!file) return;
    const body = new FormData(); body.append("file", file);
    $("calsubmit").disabled = true; showStatus("Checking the token with Google…");
    try {
      const res = await fetch("/api/calendar/token", { method: "POST", body });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      afterConnect(data);
    } catch (err) { showStatus("Upload failed: " + err.message, "err"); }
    finally { $("calsubmit").disabled = false; }
  });
  // Returning from Google's consent page: /?calendar=connected|error&reason=...
  const params = new URLSearchParams(location.search);
  if (params.get("calendar")) {
    history.replaceState(null, "", location.pathname);
    if (params.get("calendar") === "connected") refreshCalStatus().then(() => showStatus("Google Calendar connected.", "ok"));
    else refreshCalStatus().then(() => { openCalPanel(); showStatus("Google sign-in failed: " + (params.get("reason") || "unknown"), "err"); });
  }

  // ---------- websocket ----------
  const connect = () => {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.binaryType = "arraybuffer"; state.ws = ws;
    ws.onopen = sendHello;
    ws.onclose = () => {
      clearTimeout(state.idleTimer); clearTimeout(state.partialTimer); clearTimeout(state.watchdog);
      state.listening = false; state.speaking = false;
      stopCapture(); rec?.abort(); stopAudio();
      mic.classList.remove("listening", "speaking");
      $("conn").textContent = "disconnected"; $("conn").className = "pill warn";
      setTimeout(connect, 1500);
    };
    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) {
        state.speaking = true; mic.classList.add("speaking");
        const heardAt = playPCM(ev.data);
        markSound(heardAt); if (state.audioRole === "reply") markReply(heardAt);
        return;
      }
      const m = JSON.parse(ev.data);
      switch (m.type) {
        case "ready":
          $("conn").textContent = "connected · " + m.model; $("conn").className = "pill ok";
          $("ttsmode").textContent = ({ google: "Google Chirp 3 HD voice", deepgram: "Deepgram Aura-2 voice", browser: "Browser voice" })[m.tts_provider] || (m.tts_provider + " voice");
          state.sampleRate = m.sample_rate; state.serverTTS = m.tts === "cloud"; state.speculation = !!m.speculation;
          state.stt = m.stt || "browser";
          if (state.stt !== "deepgram" && !rec) { mic.disabled = true; addMsg("error", "This browser has no Web Speech API. Use Chrome, or type below."); }
          else mic.disabled = false;
          $("conn").textContent += m.stt === "deepgram" ? " · Deepgram STT" : " · browser STT"; break;
        case "transcript":
          if (!m.final) { interim.textContent = m.text; armIdleTimer(); break; }  // speech in progress: keep the mic open
          interim.textContent = ""; clearTimeout(state.idleTimer);
          // The server finished the utterance; the clock starts when the speech actually ended.
          beginTurn(m.text, performance.now() - (m.speech_end_ago_ms || 0), false);
          if (state.listening) { state.listening = false; mic.classList.remove("listening"); stopCapture(); state.ws.send(JSON.stringify({ type: "listen_stop" })); }
          break;
        case "calendar_required": refreshCalStatus(); break;
        case "ack":
          state.audioRole = "ack";
          if (!m.audio) speakBrowser(m.text, true);
          break;
        case "reply_audio_start": state.audioRole = "reply"; break;
        case "token":
          clearTimeout(state.watchdog);
          if (!state.botEl) state.botEl = addMsg("bot", ""); state.botText += m.text; state.botEl.textContent = state.botText; transcript.scrollTop = transcript.scrollHeight; break;
        case "tool_call": timing.tool = true; addTool(m.name, m.args); break;
        case "tool_result": { const last = transcript.querySelector("details.tool:last-of-type"); if (last) last.querySelector("pre").textContent += "\n→ " + JSON.stringify(m.result, null, 1); break; }
        case "turn_end":
          clearTimeout(state.watchdog);
          state.listenAfter = m.listen_after !== false;  // false after a booking: the goal is reached, keep the mic closed
          if (!state.listenAfter && state.listening) stopListening();  // e.g. the mic was still open while the user typed "book it"
          if (state.botEl) state.botEl.textContent = m.text || state.botText;
          if (!state.serverTTS) { state.speaking = true; speakBrowser(m.text); } break;
        case "audio_end":
          // No more reply audio is coming. Finish the turn now if playback already drained, else when the last chunk ends.
          state.audioDone = true;
          if (state.serverTTS && !sources.length) onSpeechDone();
          break;
        case "tts_error": if (!state.speaking) speakBrowser(state.botText); break;
        case "error":
          clearTimeout(state.watchdog);
          addMsg("error", m.message);
          onSpeechDone(); break;
      }
    };
  };

  // Start a new turn in the UI. `send` is false when the server already has the transcript (server-side STT).
  const beginTurn = (text, sentAt, send = true) => {
    text = text.trim(); if (!text || !state.ws || state.ws.readyState !== 1) return;
    stopAudio(); if (state.speaking && send) state.ws.send(JSON.stringify({ type: "cancel" }));
    state.speaking = false; mic.classList.remove("speaking");
    addMsg("user", text); state.botEl = null; state.botText = ""; state.audioRole = "reply"; state.lastPartial = ""; clearTimeout(state.partialTimer);
    resetTiming(sentAt); state.speechEndAt = 0; state.turnFinished = false; state.listenAfter = true; state.audioDone = false;
    clearTimeout(state.watchdog);
    state.watchdog = setTimeout(() => {
      if (!state.botText) addMsg("error", "The response is taking longer than expected. You can wait or try again.");
    }, 20000);
    ensureCtx(); if (send) state.ws.send(JSON.stringify({ type: "user_text", text }));
  };
  const sendText = (text) => {
    const now = performance.now();
    beginTurn(text, (state.speechEndAt && now - state.speechEndAt < 5000) ? state.speechEndAt : now);  // spoken: clock starts at end of speech
  };

  // ---------- speech recognition ----------
  // Two modes: the server transcribes the mic stream (Deepgram, any browser) or Chrome's Web Speech API.
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null;
  const capture = { stream: null, source: null, node: null };
  const stopCapture = () => {
    if (capture.node) { capture.node.port.onmessage = null; capture.node.disconnect(); capture.node = null; }
    if (capture.source) { capture.source.disconnect(); capture.source = null; }
    if (capture.stream) { capture.stream.getTracks().forEach((t) => t.stop()); capture.stream = null; }
  };
  const startCapture = async () => {
    const ac = ensureCtx();
    if (!ac.audioWorklet) throw new Error("AudioWorklet unsupported");
    if (!capture.moduleLoaded) { await ac.audioWorklet.addModule("/static/pcm-worklet.js"); capture.moduleLoaded = true; }
    // Echo cancellation removes the assistant's own voice from the mic signal.
    capture.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    capture.source = ac.createMediaStreamSource(capture.stream);
    capture.node = new AudioWorkletNode(ac, "pcm-capture");
    capture.node.port.onmessage = (e) => { if (state.listening && state.ws && state.ws.readyState === 1) state.ws.send(e.data); };
    capture.source.connect(capture.node);  // the node produces no output, so nothing is heard
    state.ws.send(JSON.stringify({ type: "listen_start", sample_rate: ac.sampleRate }));
  };
  if (SR) {
    rec = new SR(); rec.lang = "en-US"; rec.interimResults = true; rec.continuous = false; rec.maxAlternatives = 1;
    rec.onresult = (e) => {
      let finalText = "", interimText = "";
      for (const r of e.results) (r.isFinal ? (finalText += r[0].transcript) : (interimText += r[0].transcript));
      interim.textContent = interimText || finalText;
      if (finalText) { interim.textContent = ""; sendText(finalText); return; }
      // Speculative start: after the interim text has been stable for 250 ms, let the server start the model on it.
      if (state.speculation && interimText.trim() && state.ws && state.ws.readyState === 1) {
        clearTimeout(state.partialTimer);
        state.partialTimer = setTimeout(() => {
          const t = interimText.trim();
          if (t !== state.lastPartial) { state.lastPartial = t; state.ws.send(JSON.stringify({ type: "user_partial", text: t })); }
        }, 250);
      }
    };
    rec.onspeechend = () => { state.speechEndAt = performance.now(); };
    rec.onend = () => { state.listening = false; mic.classList.remove("listening"); };
    rec.onerror = (e) => { if (e.error !== "no-speech" && e.error !== "aborted") addMsg("error", "Speech recognition: " + e.error); };
  }
  const serverSTT = () => state.stt === "deepgram";
  // Close the mic after this long without speech (server-side STT has no silence limit of its own, and it bills per second).
  const LISTEN_IDLE_MS = 8000;
  const armIdleTimer = () => { clearTimeout(state.idleTimer); state.idleTimer = setTimeout(() => { if (state.listening) { interim.textContent = ""; stopListening(); } }, LISTEN_IDLE_MS); };
  const startListening = async () => {
    if (state.listening || state.startingCapture || state.ws?.readyState !== 1) return;
    if (!serverSTT() && !rec) return;
    state.ws.send(JSON.stringify({ type: "cancel" }));
    stopAudio(); state.speaking = false; mic.classList.remove("speaking"); ensureCtx();
    state.startingCapture = true;
    try {
      if (serverSTT()) await startCapture(); else rec.start();
      state.listening = true; mic.classList.add("listening");
      if (serverSTT()) armIdleTimer();
    } catch (err) { addMsg("error", "Microphone: " + (err.message || err)); stopCapture(); }
    finally { state.startingCapture = false; }
  };
  const stopListening = () => {
    if (!state.listening) return;
    clearTimeout(state.idleTimer);
    if (serverSTT()) { state.listening = false; mic.classList.remove("listening"); stopCapture(); if (state.ws?.readyState === 1) state.ws.send(JSON.stringify({ type: "listen_stop" })); }
    else rec.stop();
  };
  mic.addEventListener("click", () => (state.listening ? stopListening() : startListening()));
  document.addEventListener("keydown", (e) => { if (e.code === "Space" && e.target === document.body) { e.preventDefault(); startListening(); } });

  $("textform").addEventListener("submit", (e) => { e.preventDefault(); sendText($("textin").value); $("textin").value = ""; });
  refreshCalStatus();
  connect();
})();
// --- AXIOM HUD CONTROLLER (Zeitzone Berlin, Timer & Mikrofon-Status) ---
(function initAxiomHUD() {
  const dateEl = document.getElementById('hud-date');
  const timeEl = document.getElementById('hud-time');
  const uptimeEl = document.getElementById('hud-uptime');
  const micStatusGroup = document.getElementById('hud-mic-status');
  const micText = document.getElementById('mic-text');
  const micBtn = document.getElementById('mic');

  let activeSeconds = 0;
  let inactivityTimer = 0;
  const INACTIVITY_THRESHOLD = 30; // Nach 30 Sekunden Inaktivität setzt sich die Nutzungsdauer zurück

  // 1. Datum & Uhrzeit in Zeitzone 'Europe/Berlin'
  function updateBerlinTime() {
    const now = new Date();
    
    // Datum formatieren: Fr, 02.10.2026
    const dateOptions = { timeZone: 'Europe/Berlin', weekday: 'short', day: '2-digit', month: '2-digit', year: 'numeric' };
    const dateStr = new Intl.DateTimeFormat('de-DE', dateOptions).format(now);
    if (dateEl) dateEl.textContent = dateStr;

    // Uhrzeit formatieren: 09:54:12
    const timeOptions = { timeZone: 'Europe/Berlin', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false };
    const timeStr = new Intl.DateTimeFormat('de-DE', timeOptions).format(now);
    if (timeEl) timeEl.textContent = timeStr;
  }

  // 2. Nutzungsdauer-Timer mit Inaktivitäts-Reset
  function updateUptime() {
    activeSeconds++;
    inactivityTimer++;

    if (inactivityTimer >= INACTIVITY_THRESHOLD) {
      activeSeconds = 0; // Reset der Nutzungsdauer bei Inaktivität
    }

    const hrs = String(Math.floor(activeSeconds / 3600)).padStart(2, '0');
    const mins = String(Math.floor((activeSeconds % 3600) / 60)).padStart(2, '0');
    const secs = String(activeSeconds % 60).padStart(2, '0');

    if (uptimeEl) {
      uptimeEl.textContent = `${hrs} Std ${mins} Min ${secs} Sek`;
    }
  }

  // Inaktivität zurücksetzen bei Benutzerinteraktion
  function resetInactivity() {
    inactivityTimer = 0;
  }

  window.addEventListener('mousemove', resetInactivity);
  window.addEventListener('keydown', resetInactivity);
  window.addEventListener('click', resetInactivity);

  // 3. Mikrofon-Anzeige steuern (Nur AN wenn Aktivität vorhanden ist)
  function checkMicStatus() {
    if (micBtn && micBtn.classList.contains('listening')) {
      if (micStatusGroup) micStatusGroup.classList.add('active');
      if (micText) micText.textContent = 'AN';
    } else {
      if (micStatusGroup) micStatusGroup.classList.remove('active');
      if (micText) micText.textContent = 'AUS';
    }
  }

  // Intervalle starten
  setInterval(updateBerlinTime, 1000);
  setInterval(updateUptime, 1000);
  setInterval(checkMicStatus, 200);

  updateBerlinTime();
})();
