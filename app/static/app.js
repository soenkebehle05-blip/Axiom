(() => {
  const $ = (id) => document.getElementById(id);
  const transcript = $("transcript"), mic = $("mic"), interim = $("interim");
  const state = { ws: null, listening: false, speaking: false, sampleRate: 24000, serverTTS: true,
                  speechEndAt: 0, audioRole: "reply",
                  botEl: null, botText: "", lastPartial: "", partialTimer: null };

  const SYSTEM_LANG = "de-DE";

  const scrollToBottom = () => {
    requestAnimationFrame(() => {
      transcript.scrollTop = transcript.scrollHeight;
    });
  };

  const addMsg = (cls, text, save = false) => { 
    const el = document.createElement("div"); 
    el.className = "msg " + cls; 
    el.textContent = text; 
    transcript.appendChild(el); 
    scrollToBottom(); 
    
    if (save && (cls === "user" || cls === "bot")) {
      fetch("/api/history/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: cls, text: text })
      }).catch(() => {});
    }
    return el; 
  };

  const loadHistory = async () => {
    try {
      const res = await fetch("/api/history");
      const history = await res.json();
      if (history && history.length > 0) {
        transcript.innerHTML = "";
        history.forEach(item => {
          addMsg(item.role, item.text, false);
        });
      }
    } catch (_) {}
  };

  const addTool = (name, args) => {
    const el = document.createElement("details"); el.className = "tool";
    const summary = document.createElement("summary"), details = document.createElement("pre");
    summary.textContent = `${name}(${Object.keys(args).join(", ")})`;
    details.textContent = JSON.stringify(args, null, 1);
    el.append(summary, details);
    transcript.appendChild(el); 
    scrollToBottom(); 
    return el; 
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
    state.turnFinished = true;
    if (state.listenAfter === false) { stopListening(); return; }
    if ($("autolisten").checked && !state.listening) startListening();
  };

  const speakBrowser = (text, isAck = false) => {
    if (!text || !window.speechSynthesis) { if (!isAck) onSpeechDone(); return; }
    const u = new SpeechSynthesisUtterance(text); 
    u.lang = SYSTEM_LANG; 
    u.rate = 1.0;
    u.onstart = () => {};
    if (!isAck) u.onend = onSpeechDone;
    state.speaking = true; window.speechSynthesis.speak(u);
  };

  const drawer = $("side-drawer"), menuBtn = $("menu-toggle-btn"), drawerClose = $("drawer-close");
  const toggleDrawer = () => drawer.classList.toggle("open");
  menuBtn.addEventListener("click", toggleDrawer);
  drawerClose.addEventListener("click", () => drawer.classList.remove("open"));

  const btnPc = $("btn-mode-pc"), btnMobile = $("btn-mode-mobile");
  const setDeviceMode = (mode) => {
    if (mode === "mobile") {
      document.body.classList.remove("mode-pc");
      document.body.classList.add("mode-mobile");
      btnMobile.classList.add("active");
      btnPc.classList.remove("active");
    } else {
      document.body.classList.remove("mode-mobile");
      document.body.classList.add("mode-pc");
      btnPc.classList.add("active");
      btnMobile.classList.remove("active");
    }
  };

  btnPc.addEventListener("click", () => setDeviceMode("pc"));
  btnMobile.addEventListener("click", () => setDeviceMode("mobile"));

  const sendHello = () => { 
    if (state.ws && state.ws.readyState === 1) {
      state.ws.send(JSON.stringify({ 
        type: "hello", 
        language: "de",
        prompt_override: "Du bist AXIOM, eine zuvorkommende KI wie JARVIS. Antworte auf Deutsch und sprich mich immer mit Sir an.",
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone 
      })); 
    }
  };

  const connect = () => {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.binaryType = "arraybuffer"; state.ws = ws;
    ws.onopen = sendHello;
    ws.onclose = () => {
      state.listening = false; state.speaking = false;
      stopAudio();
      mic.classList.remove("listening", "speaking");
      setTimeout(connect, 1500);
    };
    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) {
        state.speaking = true; mic.classList.add("speaking");
        playPCM(ev.data);
        return;
      }
      const m = JSON.parse(ev.data);
      switch (m.type) {
        case "ready":
          state.sampleRate = m.sample_rate; state.serverTTS = m.tts === "cloud";
          break;
        case "transcript":
          if (!m.final) { interim.textContent = m.text; break; }
          interim.textContent = "";
          beginTurn(m.text, false);
          break;
        case "token":
          if (!state.botEl) state.botEl = addMsg("bot", "", false); 
          state.botText += m.text; 
          state.botEl.textContent = state.botText; 
          scrollToBottom(); 
          break;
        case "turn_end":
          if (state.botText) {
            fetch("/api/history/save", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ role: "bot", text: state.botText })
            }).catch(() => {});
          }
          scrollToBottom();
          if (!state.serverTTS) { state.speaking = true; speakBrowser(m.text); } break;
        case "audio_end":
          state.audioDone = true;
          if (state.serverTTS && !sources.length) onSpeechDone();
          break;
      }
    };
  };

  const beginTurn = (text, send = true) => {
    text = text.trim(); if (!text || !state.ws || state.ws.readyState !== 1) return;
    stopAudio();
    state.speaking = false; mic.classList.remove("speaking");
    addMsg("user", text, true); state.botEl = null; state.botText = "";
    ensureCtx(); if (send) state.ws.send(JSON.stringify({ type: "user_text", text }));
  };
  
  const sendText = (text) => beginTurn(text, true);

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null;
  if (SR) {
    rec = new SR(); 
    rec.lang = SYSTEM_LANG;
    rec.interimResults = true;
    rec.onresult = (e) => {
      let finalText = "", interimText = "";
      for (const r of e.results) (r.isFinal ? (finalText += r[0].transcript) : (interimText += r[0].transcript));
      interim.textContent = interimText || finalText;
      if (finalText) { interim.textContent = ""; sendText(finalText); }
    };
    rec.onend = () => { state.listening = false; mic.classList.remove("listening"); };
  }
  
  const startListening = () => {
    if (state.listening || !rec) return;
    stopAudio();
    try { rec.start(); state.listening = true; mic.classList.add("listening"); } catch (_) {}
  };
  
  const stopListening = () => { if (state.listening && rec) rec.stop(); };
  
  mic.addEventListener("click", () => (state.listening ? stopListening() : startListening()));
  $("textform").addEventListener("submit", (e) => { e.preventDefault(); sendText($("textin").value); $("textin").value = ""; });
  
  loadHistory();
  connect();
})();
