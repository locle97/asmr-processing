/* Giao diện ASMR Auto Master: thả file → phân tích/xử lý (SSE) → nghe A/B, chỉnh nhanh, tải về.
   JS thuần, không build, không thư viện ngoài, để app chạy được trên máy cá nhân khi mất mạng.
   Chạy trên Chrome và Safari (≥ 15.4): FLAC trong <audio>, Web Audio có tiền tố webkit. */
"use strict";

const AUDIO_EXT = [".wav", ".flac", ".mp3", ".m4a"];
const VIDEO_EXT = [".mp4", ".mov"];
const MAX_BYTES = 500 * 1024 * 1024;
const MIN_S = 5, MAX_S = 600;
const CEIL = Math.pow(10, -6 / 20);   // vạch −6 dB trên waveform
const XFADE_S = 0.02;                  // crossfade khi chuyển A/B
const DRIFT_S = 0.02;                  // lệch quá 20 ms thì đồng bộ lại
const DEBOUNCE_MS = 400;

const STEP_LABEL = {
  queued: "Đang chờ tới lượt…",
  decode: "Đang đọc file…",
  highpass: "Đang đo âm nền, tiếng bút và click…",
  denoise: "Đang giảm ồn…",
  gain: "Đang tăng âm lượng…",
  reporting: "Đang ghi file và viết giải thích…",
  write: "Đang ghi file và viết giải thích…",
};
const WARNING_TEXT = {
  CLIPPED: "File gốc bị clip (chạm 0 dBFS). Chỗ vỡ tiếng không sửa được, app chỉ làm nhỏ lại.",
  NO_SILENCE: "Không có đoạn yên lặng nào dài từ 0,5 giây, nên bỏ qua bước giảm ồn.",
  TOO_QUIET: "File thu quá nhỏ (dưới −45 LUFS), kết quả sẽ còn nghe rõ âm nền. Lần sau hãy tăng gain mic.",
  LOW_SNR: "Tiếng tô bút chỉ to hơn âm nền chưa tới 12 dB, kết quả sẽ còn nghe rõ âm nền. Lần sau để mic gần hơn.",
};
const ROWS = [
  // key trong report.checks, nhãn, cách lấy giá trị "trước" từ report.summary, đơn vị
  ["noise", "Âm nền", (s) => s.noise.before, "dB"],
  ["action_p95", "Tiếng bút (p95)", () => null, "dB"],
  ["action_p99", "Tiếng bút (p99)", (s) => s.action.before, "dB"],
  ["peak", "Đỉnh mẫu", (s) => s.peak.before, "dBFS"],
  ["tp", "True peak", (s) => s.tp.before, "dBTP"],
  ["lufs", "Độ to", (s) => s.lufs.before, "LUFS"],
];

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ định dạng
function num(v, digits = 1) {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  const s = Math.abs(v).toFixed(digits);
  return (v < 0 && Number(s) !== 0 ? "−" : "") + s;
}
function clock(t) {
  if (!isFinite(t)) t = 0;
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}
function range(lo, hi, unit) {
  if (lo === null || lo === undefined) return `≤ ${num(hi)} ${unit}`;
  if (hi === null || hi === undefined) return `≥ ${num(lo)} ${unit}`;
  return `${num(lo)} … ${num(hi)} ${unit}`;
}
function extOf(name) {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i).toLowerCase();
}

// ------------------------------------------------------------------ API
async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["content-type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opt);
  } catch (e) {
    throw new Error("Mất kết nối tới app. Kiểm tra lệnh asmr serve còn chạy không.");
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error((data && data.error && data.error.message) || `Lỗi ${res.status}`);
  return data;
}

function upload(file, preset, onProgress) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append("file", file, file.name);
    fd.append("preset", preset);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/jobs");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* để trống */ }
      if (xhr.status >= 200 && xhr.status < 300 && data) resolve(data);
      else reject(new Error((data && data.error && data.error.message) || `Lỗi ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("Mất kết nối tới app. Kiểm tra lệnh asmr serve còn chạy không."));
    xhr.send(fd);
  });
}

/* Đọc độ dài ngay trong trình duyệt để báo lỗi trước khi upload file lớn.
   Trình duyệt không đọc được định dạng nào đó thì trả null và để server kiểm tra. */
function probeDuration(file) {
  return new Promise((resolve) => {
    const a = document.createElement("audio");
    const url = URL.createObjectURL(file);
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      URL.revokeObjectURL(url);
      a.removeAttribute("src");
      resolve(v);
    };
    a.preload = "metadata";
    a.onloadedmetadata = () => finish(isFinite(a.duration) ? a.duration : null);
    a.onerror = () => finish(null);
    setTimeout(() => finish(null), 3000);
    a.src = url;
  });
}

async function checkFile(file) {
  const ext = extOf(file.name);
  if (VIDEO_EXT.includes(ext)) return "Xử lý video sẽ có ở bản sau. Bản này nhận file âm thanh (WAV, FLAC, MP3, M4A).";
  if (!AUDIO_EXT.includes(ext)) return `App không đọc được file ${ext || "không có đuôi"}. Hãy xuất lại thành WAV, FLAC, MP3 hoặc M4A.`;
  if (file.size > MAX_BYTES) return `File nặng ${(file.size / 1048576).toFixed(0)} MB, app nhận tối đa 500 MB. Hãy cắt bớt hoặc xuất lại nhỏ hơn.`;
  const d = await probeDuration(file);
  if (d !== null && d < MIN_S) return `File dài ${d.toFixed(1)} giây, cần ít nhất 5 giây để đo được âm nền.`;
  if (d !== null && d > MAX_S + 0.5) return `File dài ${(d / 60).toFixed(1)} phút, app nhận tối đa 10 phút. Hãy cắt bớt rồi thả lại.`;
  return null;
}

// ------------------------------------------------------------------ trình phát A/B
/* Hai <audio> chạy song song, cùng vị trí. Chuyển bản bằng 2 GainNode (crossfade 20 ms) nên không bị
   khựng. Không giải mã cả file vào bộ nhớ như AudioBuffer, file 10 phút vẫn nhẹ. Không có Web Audio
   thì chuyển bằng muted. */
class ABPlayer {
  constructor(orig, proc, onState) {
    this.el = { orig, proc };
    this.v = "proc";
    this.ctx = null;
    this.gain = {};
    this.playing = false;
    this.onState = onState;
    this.timer = null;
    this.applyMute();
    proc.addEventListener("ended", () => this.pause());
    orig.addEventListener("ended", () => this.pause());
  }

  get active() { return this.el[this.v]; }
  get other() { return this.el[this.v === "orig" ? "proc" : "orig"]; }
  get time() { return this.active.currentTime || 0; }
  get duration() {
    const d = this.active.duration;
    return isFinite(d) ? d : 0;
  }

  ensureContext() {
    if (this.ctx || this.noWebAudio) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) { this.noWebAudio = true; return; }
    try {
      const ctx = new AC();
      for (const k of ["orig", "proc"]) {
        const g = ctx.createGain();
        g.gain.value = k === this.v ? 1 : 0;
        ctx.createMediaElementSource(this.el[k]).connect(g);
        g.connect(ctx.destination);
        this.gain[k] = g;
        this.el[k].muted = false;
      }
      this.ctx = ctx;
    } catch (e) {
      this.noWebAudio = true;
    }
  }

  applyMute() {
    if (this.ctx) return;
    this.el.orig.muted = this.v !== "orig";
    this.el.proc.muted = this.v !== "proc";
  }

  select(v) {
    if (v === this.v) return;
    this.other.currentTime = this.active.currentTime;
    this.v = v;
    if (this.ctx) {
      const t = this.ctx.currentTime;
      for (const k of ["orig", "proc"]) {
        const p = this.gain[k].gain;
        p.cancelScheduledValues(t);
        p.setValueAtTime(p.value, t);
        p.linearRampToValueAtTime(k === v ? 1 : 0, t + XFADE_S);
      }
    } else {
      this.applyMute();
    }
    this.onState();
  }

  async play() {
    this.ensureContext();  // phải gọi trong thao tác của người dùng (Safari)
    if (this.ctx && this.ctx.state === "suspended") await this.ctx.resume();
    this.other.currentTime = this.active.currentTime;
    this.playing = true;
    this.onState();
    try {
      await Promise.all([this.el.orig.play(), this.el.proc.play()]);
    } catch (e) {
      this.pause();
      return;
    }
    clearInterval(this.timer);
    this.timer = setInterval(() => this.resync(), 500);
  }

  pause() {
    this.el.orig.pause();
    this.el.proc.pause();
    clearInterval(this.timer);
    this.playing = false;
    this.onState();
  }

  toggle() { return this.playing ? this.pause() : this.play(); }

  resync() {
    if (Math.abs(this.other.currentTime - this.active.currentTime) > DRIFT_S) {
      this.other.currentTime = this.active.currentTime;
    }
  }

  seek(t) {
    t = Math.max(0, Math.min(t, this.duration || t));
    this.el.orig.currentTime = t;
    this.el.proc.currentTime = t;
    this.onState();
  }

  /* Đổi sang kết quả của lần render khác, giữ vị trí và trạng thái phát. */
  load(origUrl, procUrl) {
    const t = this.time, was = this.playing;
    if (was) this.pause();
    const ready = (el) => new Promise((resolve) => {
      if (el.readyState >= 1) return resolve();
      el.addEventListener("loadedmetadata", resolve, { once: true });
      el.addEventListener("error", resolve, { once: true });
    });
    this.el.orig.src = origUrl;
    this.el.proc.src = procUrl;
    this.el.orig.load();
    this.el.proc.load();
    return Promise.all([ready(this.el.orig), ready(this.el.proc)]).then(() => {
      if (t > 0) this.seek(t);
      if (was) this.play();
      this.onState();
    });
  }
}

// ------------------------------------------------------------------ waveform
/* Vẽ từ peaks tính sẵn ở server (4000 điểm mỗi kênh), cùng thang tuyến tính ±1, vạch ±0.501 = −6 dB. */
class Wave {
  constructor(canvas, colorVar) {
    this.c = canvas;
    this.colorVar = colorVar;
    this.peaks = null;
    this.duration = 0;
  }

  set(data) {
    const ch = data.channels;
    const mono = new Float32Array(data.length);
    for (let i = 0; i < data.length; i++) {
      let m = 0;
      for (const c of ch) m = Math.max(m, c[i]);
      mono[i] = m;
    }
    this.peaks = mono;
    this.duration = data.duration;
  }

  draw(pos) {
    const c = this.c, dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(c.clientWidth * dpr)), h = Math.max(1, Math.round(c.clientHeight * dpr));
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    const g = c.getContext("2d");
    const css = getComputedStyle(document.documentElement);
    g.clearRect(0, 0, w, h);
    const mid = h / 2;
    if (this.peaks) {
      const p = this.peaks, n = p.length;
      g.fillStyle = css.getPropertyValue(this.colorVar).trim();
      for (let x = 0; x < w; x++) {
        const a = Math.floor((x / w) * n), b = Math.max(a + 1, Math.floor(((x + 1) / w) * n));
        let m = 0;
        for (let i = a; i < b && i < n; i++) if (p[i] > m) m = p[i];
        const y = Math.max(1, m * mid);
        g.fillRect(x, mid - y, 1, 2 * y);
      }
    }
    g.strokeStyle = css.getPropertyValue("--ink2").trim();
    g.lineWidth = dpr;
    g.setLineDash([4 * dpr, 4 * dpr]);
    for (const y of [mid - CEIL * mid, mid + CEIL * mid]) {
      g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke();
    }
    g.setLineDash([]);
    if (this.duration > 0) {
      const x = Math.round((pos / this.duration) * w);
      g.strokeStyle = css.getPropertyValue("--fg").trim();
      g.lineWidth = 2 * dpr;
      g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
    }
  }
}

// ------------------------------------------------------------------ trạng thái app
const state = {
  jobId: null,
  rid: null,          // lần render đang hiển thị
  pending: null,      // lần render đang chờ
  report: null,
  es: null,
  params: { preset: "default", loudness: 0, denoise: null, floor: null },
  debounce: null,
};

const player = new ABPlayer($("audio-orig"), $("audio-proc"), () => updatePlayerUi());
const waves = { orig: new Wave($("wave-orig"), "--wave-orig"), proc: new Wave($("wave-proc"), "--wave-proc") };

function show(id, on = true) { $(id).hidden = !on; }
function setError(id, msg) {
  $(id).textContent = msg || "";
  show(id, !!msg);
}

function reset() {
  if (state.es) state.es.close();
  clearTimeout(state.debounce);
  player.pause();
  Object.assign(state, { jobId: null, rid: null, pending: null, report: null, es: null });
  state.params = { preset: state.params.preset, loudness: 0, denoise: null, floor: null };
  show("dropzone", true);
  show("file-chip", false);
  show("upload-bar", false);
  show("analysis-section", false);
  show("result-section", false);
  setError("drop-error", null);
  setError("job-error", null);
  $("file-input").value = "";
  for (const k of ["stat-noise", "stat-action", "stat-click"]) $(k).textContent = "…";
  $("stat-click-count").textContent = "bật nắp, gõ bút";
  $("warnings").innerHTML = "";
}

// ------------------------------------------------------------------ màn 1: thả file
async function handleFile(file) {
  if (!file) return;
  setError("drop-error", null);
  const problem = await checkFile(file);
  if (problem) return setError("drop-error", problem);

  $("file-name").textContent = file.name;
  show("dropzone", false);
  show("file-chip", true);
  show("upload-bar", true);
  const fill = $("upload-bar").firstElementChild;
  fill.style.width = "0";
  try {
    const res = await upload(file, state.params.preset, (f) => { fill.style.width = `${(f * 100).toFixed(1)}%`; });
    show("upload-bar", false);
    state.jobId = res.job_id;
    show("analysis-section", true);
    follow(res.render_id);
    updateJob(res.job, res.render_id);
  } catch (e) {
    reset();
    setError("drop-error", e.message);
  }
}

function setupDrop() {
  const dz = $("dropzone");
  $("file-input").addEventListener("change", (e) => handleFile(e.target.files[0]));
  dz.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file-input").click(); }
  });
  for (const t of ["dragenter", "dragover"]) {
    dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); });
  }
  for (const t of ["dragleave", "drop"]) dz.addEventListener(t, () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    handleFile(e.dataTransfer.files[0]);
  });
  // thả file ra ngoài vùng thả thì trình duyệt sẽ mở file đó, chặn lại
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());
  $("new-file").addEventListener("click", reset);
}

// ------------------------------------------------------------------ màn 2: phân tích, tiến độ
function follow(rid) {
  state.pending = rid;
  if (state.es) state.es.close();
  const es = new EventSource(`/jobs/${state.jobId}/events`);
  state.es = es;
  es.addEventListener("job", (e) => updateJob(JSON.parse(e.data), rid));
  es.addEventListener("end", () => es.close());
  es.onerror = () => {
    // mất kết nối SSE (server khởi động lại, máy ngủ): thử lại sau 2 s nếu vẫn đang chờ lần render này
    es.close();
    setTimeout(() => {
      if (state.es === es && state.pending === rid) follow(rid);
    }, 2000);
  };
}

function updateJob(job, rid) {
  if (rid !== state.pending) return;
  const a = job.analysis;
  if (a) {
    $("stat-noise").textContent = `${num(a.N_db)} dB`;
    $("stat-action").textContent = `${num(a.A_db)} dB`;
    $("stat-click").textContent = `${num(a.C_peak_dbfs)} dBFS`;
    $("stat-click-count").textContent = `${a.C_big} đỉnh lớn, ${a.C_mid} đỉnh vừa`;
  }
  const ul = $("warnings");
  ul.innerHTML = "";
  for (const w of job.warnings || []) {
    const li = document.createElement("li");
    li.textContent = WARNING_TEXT[w] || w;
    ul.appendChild(li);
  }

  const r = job.renders[rid];
  if (!r) return;
  if (r.status === "failed") {
    show("progress", false);
    show("rerender", false);
    document.body.classList.remove("busy");
    setError("job-error", (r.error && r.error.message) || "Xử lý lỗi.");
    state.pending = null;
    return;
  }
  setError("job-error", null);
  if (r.status === "done") {
    state.pending = null;
    if (state.es) state.es.close();
    loadResult(rid);
    return;
  }
  show("progress", true);
  $("progress-fill").style.width = `${Math.round((r.progress || 0) * 100)}%`;
  const m = /^render_round_(\d+)/.exec(r.step || "");
  $("progress-label").textContent = m
    ? `Đang xử lý 7 bước và đo lại (vòng ${m[1]}/3)…`
    : STEP_LABEL[r.step] || STEP_LABEL[r.status] || "Đang xử lý…";
}

async function requestRender() {
  if (!state.jobId) return;
  setError("job-error", null);
  show("rerender", !!state.rid);
  if (state.rid) document.body.classList.add("busy");
  try {
    const res = await api("POST", `/jobs/${state.jobId}/renders`, state.params);
    if (res.render.status === "done") {  // thông số đã render trước đó: lấy lại kết quả cũ
      if (state.es) state.es.close();
      state.pending = null;
      await loadResult(res.render_id);
    } else {
      follow(res.render_id);
    }
  } catch (e) {
    show("rerender", false);
    document.body.classList.remove("busy");
    setError("job-error", e.message);
  }
}

function scheduleRender() {
  clearTimeout(state.debounce);
  state.debounce = setTimeout(requestRender, DEBOUNCE_MS);
}

function setupPresets() {
  for (const r of document.querySelectorAll('input[name="preset"]')) {
    r.addEventListener("change", () => {
      state.params.preset = r.value;
      if (state.jobId) scheduleRender();
    });
  }
}

// ------------------------------------------------------------------ màn 3: kết quả
async function loadResult(rid) {
  const base = `/jobs/${state.jobId}/renders/${rid}`;
  const [{ report }, peaks] = await Promise.all([api("GET", base), api("GET", `${base}/peaks`)]);
  if (state.pending && state.pending !== rid) return;  // đã có lần render mới hơn
  state.rid = rid;
  state.report = report;
  show("progress", false);
  show("rerender", false);
  document.body.classList.remove("busy");
  show("result-section", true);

  waves.orig.set(peaks.before);
  waves.proc.set(peaks.after);
  await player.load(`${base}/audio?v=orig`, `${base}/audio?v=proc`);
  renderMetrics(report);
  $("explanation").textContent = (report.explanation && report.explanation.text) || "";
  syncSliders(report);
  $("dl-wav").href = `${base}/download?f=wav`;
  $("dl-json").href = `${base}/download?f=json`;
  $("dl-png").href = `${base}/download?f=png`;
  $("dl-wav").textContent = `Tải ${report.output.wav}`;
  updatePlayerUi();
}

function renderMetrics(report) {
  const summary = Object.fromEntries(report.summary.map((s) => [s.key, s]));
  const checks = Object.fromEntries(report.checks.map((c) => [c.key, c]));
  const tb = $("metrics");
  tb.innerHTML = "";
  for (const [key, label, before, unit] of ROWS) {
    const c = checks[key];
    if (!c) continue;
    const tr = document.createElement("tr");
    const target = key === "peak" ? summary.peak.target : c.target;
    const cells = [label, `${num(before(summary))}`, `${num(c.value)} ${unit}`, range(target[0], target[1], unit),
                   c.pass ? "✓ đạt" : "✗ chưa đạt"];
    cells.forEach((t, i) => {
      const td = document.createElement("td");
      td.textContent = t;
      if (i === 4) td.className = c.pass ? "pass" : "fail";
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  }
}

/* Thanh trượt chưa chạm thì hiện giá trị quy tắc đã chọn ("tự động"). */
function syncSliders(report) {
  const p = state.params;
  const auto = { denoise: report.params.denoise.db, floor: report.params.expander.max_cut_db };
  $("sl-loudness").value = p.loudness;
  $("out-loudness").textContent = `${p.loudness > 0 ? "+" : ""}${num(p.loudness, 0)} LU`;
  for (const k of ["denoise", "floor"]) {
    const v = p[k] === null ? auto[k] : p[k];
    $(`sl-${k}`).value = Math.round(v);
    $(`out-${k}`).textContent = p[k] === null ? `tự động (${num(v)} dB)` : `${num(v, 0)} dB`;
  }
}

function setupSliders() {
  const fmt = { loudness: (v) => `${v > 0 ? "+" : ""}${num(v, 0)} LU`, denoise: (v) => `${num(v, 0)} dB`, floor: (v) => `${num(v, 0)} dB` };
  for (const k of ["loudness", "denoise", "floor"]) {
    const el = $(`sl-${k}`);
    el.addEventListener("input", () => { $(`out-${k}`).textContent = fmt[k](Number(el.value)); });
    // "change" chỉ bắn khi thả tay, nên kéo không gửi hàng loạt yêu cầu
    el.addEventListener("change", () => {
      state.params[k] = Number(el.value);
      scheduleRender();
    });
  }
  $("reset").addEventListener("click", () => {
    Object.assign(state.params, { loudness: 0, denoise: null, floor: null });
    scheduleRender();
  });
}

function updatePlayerUi() {
  $("play").textContent = player.playing ? "❚❚" : "▶";
  $("play").setAttribute("aria-label", player.playing ? "Dừng" : "Phát");
  for (const b of document.querySelectorAll(".ab button")) {
    b.classList.toggle("on", b.dataset.v === player.v);
    b.setAttribute("aria-pressed", String(b.dataset.v === player.v));
  }
  for (const w of document.querySelectorAll(".wave")) w.classList.toggle("on", w.dataset.v === player.v);
  drawFrame();
}

function drawFrame() {
  const t = player.time;
  $("time").textContent = `${clock(t)} / ${clock(player.duration || waves.proc.duration)}`;
  waves.orig.draw(t);
  waves.proc.draw(t);
}

function loop() {
  if (player.playing && !$("result-section").hidden) drawFrame();
  requestAnimationFrame(loop);
}

function setupPlayer() {
  $("play").addEventListener("click", () => player.toggle());
  for (const b of document.querySelectorAll(".ab button")) b.addEventListener("click", () => player.select(b.dataset.v));
  for (const v of ["orig", "proc"]) {
    const c = $(`wave-${v}`);
    c.addEventListener("pointerdown", (e) => {
      const r = c.getBoundingClientRect();
      player.seek(((e.clientX - r.left) / r.width) * (player.duration || waves[v].duration));
    });
  }
  document.addEventListener("keydown", (e) => {
    if ($("result-section").hidden || e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = e.target.tagName;
    if (tag === "INPUT" && e.target.type !== "range" && e.target.type !== "radio") return;
    if (e.code === "Space") {
      e.preventDefault();  // không cuộn trang, không bấm nút đang focus
      player.toggle();
    } else if (e.key === "a" || e.key === "A") {
      player.select("orig");
    } else if (e.key === "b" || e.key === "B") {
      player.select("proc");
    }
  });
  // Safari bấm nút bằng Space ở keyup, chặn để không phát/dừng 2 lần
  document.addEventListener("keyup", (e) => {
    if (e.code === "Space" && !$("result-section").hidden && e.target.tagName === "BUTTON") e.preventDefault();
  });
  window.addEventListener("resize", drawFrame);
  requestAnimationFrame(loop);
}

setupDrop();
setupPresets();
setupSliders();
setupPlayer();
