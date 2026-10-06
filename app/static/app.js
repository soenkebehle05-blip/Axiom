// Browser side of the voice loop: Web Speech API (STT) -> WebSocket -> streamed text + PCM audio.
(() => {
  const $ = (id) => document.getElementById(id);
  const transcript = $("transcript"), mic = $("mic"), interim = $("interim");
  const state = { ws: null, listening: false, speaking: false, sampleRate: 24000, serverTTS: true,
                  speechEndAt: 0, audioRole: "reply",
                  botEl: null, botText: "", lastPartial: "", partialTimer: null, closeWindowAfterSpeech: false };

  // ---------- briefing music ----------
  let briefingAudio = null;
  const playBriefingMusic = () => {
    try {
      if (!briefingAudio) {
        briefingAudio = new Audio('/static/briefing_music.mp3');
        briefingAudio.volume = 0.15;
        briefingAudio.loop = true; // Musik in Endlosschleife abspielen
      }
      briefingAudio.currentTime = 0;
      const p = briefingAudio.play();
      if (p !== undefined) {
        p.catch((err) => console.log("Audio play deferred or blocked:", err));
      }
    } catch (e) {
      console.log("Briefing audio error:", e);
    }
  };
  const stopBriefingMusic = () => {
    if (briefingAudio) {
      briefingAudio.pause();
      briefingAudio.currentTime = 0;
    }
  };

  // ---------- turn timing (console only) ----------
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
    // stopBriefingMusic(); <-- HIER ENTFERNT, damit die Musik weiterläuft!
    if (state.turnFinished) return;
    state.turnFinished = true; logTurnTiming();

    if (state.closeWindowAfterSpeech) {
      setTimeout(() => {
        window.close();
        window.location.href = "about:blank";
      }, 500);
      return;
    }

    if (state.listenAfter === false) { stopListening(); return; }
    if ($("autolisten").checked && !state.listening) startListening();
  };

  // ---------- Notizen Dashboard ----------
  const addNoteBtn = $("add-note-btn");
  const addNoteForm = $("add-note-form");
  const newNoteInput = $("new-note-input");
  const saveNoteBtn = $("save-note-btn");

  if (addNoteBtn && addNoteForm) {
    addNoteBtn.addEventListener("click", () => {
      addNoteForm.hidden = !addNoteForm.hidden;
      if (!addNoteForm.hidden && newNoteInput) {
        newNoteInput.focus();
      }
    });
  }

  const handleSaveNote = () => {
    if (!newNoteInput) return;
    const val = newNoteInput.value.trim();
    if (val) {
      sendText(`Notiz hinzufügen: ${val}`);
      newNoteInput.value = "";
      addNoteForm.hidden = true;
    }
  };

  if (saveNoteBtn) {
    saveNoteBtn.addEventListener("click", handleSaveNote);
  }
  if (newNoteInput) {
    newNoteInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleSaveNote();
      }
    });
  }

  // Restlicher Code deiner app.js bleibt unverändert...
