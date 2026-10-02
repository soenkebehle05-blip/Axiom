(() => {
  const $ = (id) => document.getElementById(id);
  const transcript = $("transcript"), mic = $("mic"), interim = $("interim");
  const state = { ws: null, listening: false, speaking: false, sampleRate: 24000, serverTTS: true,
                  speechEndAt: 0, audioRole: "reply",
                  botEl: null, botText: "", lastPartial: "", partialTimer: null };

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

  const addMsg = (cls, text) => { const el = document.createElement("div"); el.className = "msg " + cls; el.textContent = text; transcript.appendChild(el); transcript.scrollTop = transcript.scrollHeight; return el; };
  const addTool = (name, args) => {
    const el = document.createElement("details"); el.className = "tool";
    const summary = document.createElement("summary"), details = document.createElement("pre");
    summary.textContent = `${name}(${Object.keys(args).join(", ")})`;
    details.textContent = JSON.stringify(args, null, 1);
    el.append(summary, details);
    transcript.appendChild(el); transcript.scrollTop = transcript.scrollHeight; return el;
  };

  let ctx = null, nextTime = 0, sources = [];
  const ensureCtx = () => { if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: state.sampleRate }); if (ctx.state === "suspended") ctx.resume(); return ctx; };
  const playPCM = (buf) => {
    const ac = ensureCtx();
    if (buf.byteLength % 2) buf = buf.slice(0, buf.byteLength - 1);
    const i16 = new Int16Array(buf); const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    const audio = ac.createBuffer(1, f32.length, state.sampleRate); audio.copyToChannel(f32, 0);
    const src = ac.createBufferSource(); src.buffer = audio; src.connect(ac.destination);
    const startAt = Math.max(ac.currentTime + 0.02, nextTime); src.start(startAt); nextTime = startAt + audio.duration;
    sources.push(src); src.onended = () => { sources = sources.filter((s) => s !== src); if (!sources.length && state.audioDone) onSpeechDone(); };
    return performance.now() + (startAt - ac.currentTime) * 1000;
  };
  const stopAudio = () => { sources.forEach((s) => { s.onended = null; try { s.stop(); } catch (_) {} }); sources = []; nextTime = 0; window.speechSynthesis?.cancel(); };
  
  const onSpeechDone = () => {
    state.speaking = false; mic.classList.remove("speaking");
    if (state.turnFinished) return;
    state.turnFinished = true; logTurnTiming();
    if (state.listenAfter === false) { stopListening(); return; }
    if ($("autolisten").checked && !state.listening) startListening();
  };

  const speakBrowser = (text, isAck = false) => {
    if (!text || !window.speechSynthesis) { if (!isAck) onSpeechDone(); return; }
    const u = new SpeechSynthesisUtterance(text); u.rate = 1.05;
    u.onstart = () => { const now = performance.now(); markSound(now); if (!isAck) markReply(now); };
    if (!isAck) u.onend = onSpeechDone;
    state.speaking = true; window.speechSynthesis.speak(u);
  };

  const cal = { connected: false, source: "none", oauth: false };
  const calPanel = $("calpanel"), calStatus = $("calstatus");
  const setInputsEnabled = (on) => { mic.disabled = !on || (state.stt !== "deepgram" && !rec); $("textin").disabled = !on; };
  const showStatus = (text, cls = "") => { calStatus.textContent = text; calStatus.className = "hint status " + cls; };
  const openCalPanel = () => {
    $("calpanel-title").textContent = cal.source === "you" ? "Ihr Google Calendar" : cal.connected ? "Eigenen Google Calendar nutzen" : "Google Calendar verbinden";
    $("calsignin").textContent = cal.source === "you" ? "Konto wechseln" : "Mit Google anmelden";
    $("calsignin").hidden = !cal.oauth; $("caldisconnect").hidden = cal.source !== "you"; $("calclose").hidden = !cal.connected;
    showStatus(cal.oauth ? "" : "Google-Anmeldung nicht konfiguriert; Laden Sie stattdessen ein Token hoch.");
    calPanel.hidden = false;
  };
  const renderCalStatus = (s) => {
    cal.connected = !!s.connected; cal.source = s.source; cal.oauth = !!s.oauth_available;
    const who = s.calendar && (s.calendar.summary || s.calendar.id);
    const label = !cal.connected ? "Calendar: Nicht verbunden"
                : cal.source === "you" ? "Calendar: " + who : "Calendar: " + (who || "Standard");
    $("calmode").textContent = label; $("calmode").className = "pill clickable " + (cal.connected ? "ok" : "warn");
    $("calpanel-current").hidden = !cal.connected;
    if (cal.connected) $("calpanel-current").textContent = "Verbunden als: " + who;
    setInputsEnabled(cal.connected);
    if (!cal.connected) openCalPanel();
  };
  const refreshCalStatus = async () => { try { renderCalStatus(await (await fetch("/api/calendar/status")).json()); } catch (_) {} };
  const sendHello = () => { if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify({ type: "hello", timezone: Intl.DateTimeFormat().resolvedOptions().timeZone })); };
  
  $("calmode").addEventListener("click", openCalPanel);
  $("calclose").addEventListener("click", () => { calPanel.hidden = true; });
  $("calsignin").addEventListener("click", () => { location.href = "/api/calendar/oauth/start"; });
  $("caldisconnect").addEventListener("click", async () => {
    try { const res = await fetch("/api/calendar/token", { method: "DELETE" }); const data = await res.json(); renderCalStatus(data); sendHello(); }
    catch (err) { showStatus("Fehler beim Trennen: " + err.message, "err"); }
  });

  // Handler für den Chat-Löschen-Button
  $("clearchat").addEventListener("click", async () => {
    if (!confirm("Möchten Sie den gesamten Chatverlauf wirklich löschen, Sir?")) return;
    try {
      const res = await fetch("/api/chat/clear", { method: "DELETE" });
      if (res.ok) {
        transcript.innerHTML = '<div class="msg bot">Sehr wohl, Sir. Der Chatverlauf wurde vollständig gelöscht. Wie kann ich Ihnen zu Diensten sein?</div>';
      } else {
        addMsg("error", "Fehler beim Löschen des Chatverlaufs.");
      }
    } catch (err) {
      addMsg("error", "Verbindungsfehler: " + err.message);
    }
  });

  const connect = () => {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.binaryType = "arraybuffer"; state.ws = ws;
    ws.onopen = sendHello;
    ws.onclose = () => {
      clearTimeout(state.idleTimer); clearTimeout(state.partialTimer); clearTimeout(state.watchdog);
      state.listening = false; state.speaking = false;
      stopCapture(); rec?.abort(); stopAudio();
      mic.classList.remove("listening", "speaking");
      $("conn").textContent = "getrennt"; $("conn").className = "pill warn";
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
          $("conn").textContent = "verbunden"; $("conn").className = "pill ok";
          state.sampleRate = m.sample_rate; state.serverTTS = m.tts === "cloud"; state.speculation = !!m.speculation;
          state.stt = m.stt || "browser";
          mic.disabled = false;
          break;
        case "transcript":
          if (!m.final) { interim.textContent = m.text; armIdleTimer(); break; }
          interim.textContent = ""; clearTimeout(state.idleTimer);
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
          state.listenAfter = m.listen_after !== false;
          if (!state.listenAfter && state.listening) stopListening();
          if (state.botEl) state.botEl.textContent = m.text || state.botText;
          if (!state.serverTTS) { state.speaking = true; speakBrowser(m.text); } break;
        case "audio_end":
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

  const beginTurn = (text, sentAt, send = true) => {
    text = text.trim(); if (!text || !state.ws || state.ws.readyState !== 1) return;
    stopAudio(); if (state.speaking && send) state.ws.send(JSON.stringify({ type: "cancel" }));
    state.speaking = false; mic.classList.remove("speaking");
    addMsg("user", text); state.botEl = null; state.botText = ""; state.audioRole = "reply"; state.lastPartial = ""; clearTimeout(state.partialTimer);
    resetTiming(sentAt); state.speechEndAt = 0; state.turnFinished = false; state.listenAfter = true; state.audioDone = false;
    clearTimeout(state.watchdog);
    state.watchdog = setTimeout(() => {
      if (!state.botText) addMsg("error", "Die Antwort dauert länger als erwartet.");
    }, 20000);
    ensureCtx(); if (send) state.ws.send(JSON.stringify({ type: "user_text", text }));
  };
  const sendText = (text) => {
    const now = performance.now();
    beginTurn(text, (state.speechEndAt && now - state.speechEndAt < 5000) ? state.speechEndAt : now);
  };

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
    capture.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    capture.source = ac.createMediaStreamSource(capture.stream);
    capture.node = new AudioWorkletNode(ac, "pcm-capture");
    capture.node.port.onmessage = (e) => { if (state.listening && state.ws && state.ws.readyState === 1) state.ws.send(e.data); };
    capture.source.connect(capture.node);
    state.ws.send(JSON.stringify({ type: "listen_start", sample_rate: ac.sampleRate }));
  };
  if (SR) {
    rec = new SR(); rec.lang = "de-DE"; rec.interimResults = true; rec.continuous = false; rec.maxAlternatives = 1;
    rec.onresult = (e) => {
      let finalText = "", interimText = "";
      for (const r of e.results) (r.isFinal ? (finalText += r[0].transcript) : (interimText += r[0].transcript));
      interim.textContent = interimText || finalText;
      if (finalText) { interim.textContent = ""; sendText(finalText); return; }
    };
    rec.onspeechend = () => { state.speechEndAt = performance.now(); };
    rec.onend = () => { state.listening = false; mic.classList.remove("listening"); };
  }
  const serverSTT = () => state.stt === "deepgram";
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
    } catch (err) { addMsg("error", "Mikrofon: " + (err.message || err)); stopCapture(); }
    finally { state.startingCapture = false; }
  };
  const stopListening = () => {
    if (!state.listening) return;
    clearTimeout(state.idleTimer);
    if (serverSTT()) { state.listening = false; mic.classList.remove("listening"); stopCapture(); if (state.ws?.readyState === 1) state.ws.send(JSON.stringify({ type: "listen_stop" })); }
    else rec.stop();
  };
  mic.addEventListener("click", () => (state.listening ? stopListening() : startListening()));

  $("textform").addEventListener("submit", (e) => { e.preventDefault(); sendText($("textin").value); $("textin").value = ""; });
  refreshCalStatus();
  connect();
})();
