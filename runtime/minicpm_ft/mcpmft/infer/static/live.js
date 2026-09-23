'use strict';
/**
 * gnsis live — the phone surface over GNSIS's existing WebSocket runtime.
 *
 * This adds no transport and no protocol. It speaks exactly what the desktop
 * client already speaks: `/ws/duplex` for PCM16 audio both ways, `/ws/screen`
 * for JPEG frames behind a per-session token, and the same event vocabulary.
 *
 * Two rules shape the whole file.
 *
 * It never claims sight before the server has said so. "Session ready"
 * appears on the first `screen.frame.accepted` and not one moment earlier —
 * sending a frame is not evidence that a frame arrived.
 *
 * And End means off. Tracks stopped, sockets closed, worklet released,
 * playback dropped, so the phone's own camera and microphone indicators go out.
 * The same holds for the camera switch inside a session: Camera off stops the
 * track and tells the runtime to go back to voice, rather than blanking the
 * picture while the lens stays live.
 */

const INPUT_RATE = 16000;
const JPEG_QUALITY = 0.72;
const MAX_EDGE = 640;
// A shared screen is mostly text and edges, which a small or soft JPEG turns
// to mush. The live model scales every frame down to 448 px anyway; a back
// brain, where one is configured, reads the frame as sent. So screens go up
// larger and cleaner than camera frames — not so large that a home upload
// chokes at two frames a second.
const SCREEN_MAX_EDGE = 1280;
const SCREEN_JPEG_QUALITY = 0.8;
// How long a cold start may look like nothing before the page says so in
// words. The runtime reports `runtime.status` while it loads the model onto a
// GPU, which takes minutes from cold. The wait is not cancelled when this
// fires — it goes on underneath, and a session that becomes ready while the
// notice is up simply starts.
const WAKING_NOTICE_MS = 35000;
// "Session ready." has done its job once the picture is live; after this long
// it steps out of the way of the thing being looked at.
const SETTLE_MS = 2400;
// How long "Session ended." stays up, over a Night stage with gnsis's eyes
// closed, before the landing comes back. Long enough to read, no longer.
const ENDED_HOLD_MS = 900;
// Rear camera by preference: someone pointing a phone at a thing wants the
// lens on the far side. `ideal` rather than `exact` so a laptop or a phone
// without a rear camera still works instead of throwing.
/**
 * The browser's own picker, opened on the Tabs list: most of what people want
 * gnsis to look at is one tab. A window or the whole screen is one click
 * away in the same picker. This page itself is left out of the list —
 * sharing it would show gnsis a picture of itself — and Chrome's "Share this
 * tab instead" stays on, so switching tabs mid-session needs no new picker.
 */
const SCREEN_OPTIONS = Object.freeze({
  video: { displaySurface: 'browser', frameRate: { ideal: 5, max: 10 } },
  audio: false,
  selfBrowserSurface: 'exclude',
  surfaceSwitching: 'include',
  monitorTypeSurfaces: 'include',
});

/** Screen sharing needs a browser that can do it and a real pointer. */
const canShareScreen = Boolean(
  navigator.mediaDevices
  && typeof navigator.mediaDevices.getDisplayMedia === 'function'
  && window.matchMedia('(pointer: fine)').matches
);

const VIDEO_CONSTRAINTS = Object.freeze({
  facingMode: { ideal: 'environment' },
  width: { ideal: 1280 },
  height: { ideal: 720 },
});

const ui = {
  root: document.getElementById('live'),
  permission: document.getElementById('permission'),
  allow: document.getElementById('allow'),
  cancel: document.getElementById('cancel'),
  useThis: document.getElementById('useThis'),
  useScreen: document.getElementById('useScreen'),
  askTitle: document.getElementById('askTitle'),
  askBody: document.getElementById('askBody'),
  preview: document.getElementById('preview'),
  scratch: document.getElementById('scratch'),
  ghost: document.getElementById('ghost'),
  status: document.getElementById('status'),
  statusText: document.getElementById('statusText'),
  hapticsSwitch: document.getElementById('haptics'),
  says: document.getElementById('says'),
  start: document.getElementById('start'),
  end: document.getElementById('end'),
  mute: document.getElementById('mute'),
  muteLabel: document.getElementById('muteLabel'),
  camToggle: document.getElementById('camToggle'),
  share: document.getElementById('share'),
  shareLabel: document.getElementById('shareLabel'),
  voice: document.getElementById('voice'),
  notice: document.getElementById('notice'),
  waking: document.getElementById('waking'),
  wakingWait: document.getElementById('wakingWait'),
  wakingRetry: document.getElementById('wakingRetry'),
};

/** Everything a running session owns, so End can let go of all of it. */
let live = null;

/** Pending timer for the cold-start notice, so every exit can clear it. */
let wakingTimer = null;

/** Where focus was before the notice took it, so it can be handed back. */
let wakingReturnFocus = null;

/** Set once the person has said they will wait, so this session stops asking. */
let wakingDismissed = false;

/**
 * The runtime is loading its model. Say so, and if the wait goes on long
 * enough to look broken, say it in words as well.
 */
function wakingUp() {
  show('GNSIS is waking up…', { state: 'connecting' });
  // Keep waiting has to mean keep waiting. The runtime heartbeats every
  // fifteen seconds and each one lands here, so without the dismissed flag
  // the next one would arm a fresh timer and the sheet somebody just closed
  // would be back thirty-five seconds later — again and again, for the whole
  // of exactly the multi-minute load this is meant to make bearable.
  if (wakingDismissed || wakingTimer !== null) return;
  wakingTimer = setTimeout(() => {
    wakingTimer = null;
    // `ready`, a close and End all clear this first, so reaching here means
    // the wait really is still going.
    if (live && !live.ready && !live.stopped) openWaking();
  }, WAKING_NOTICE_MS);
}

/**
 * Show the notice and move into it.
 *
 * Unhiding an aria-modal dialog neither announces it nor moves the keyboard.
 * Focus would stay on whatever sits behind the sheet, so somebody reading the
 * screen or driving it from a keyboard would never learn that the notice, or
 * either of its buttons, was there.
 */
function openWaking() {
  if (!ui.waking.hidden) return;
  wakingReturnFocus = document.activeElement;
  ui.waking.hidden = false;
  ui.wakingWait.focus();
}

/** Close the notice, handing focus back if there is still somewhere to hand it. */
function closeWaking() {
  if (ui.waking.hidden) return;
  ui.waking.hidden = true;
  const back = wakingReturnFocus;
  wakingReturnFocus = null;
  // End replaces the whole view, so what focus came from may be gone by now.
  if (back && back.isConnected && typeof back.focus === 'function') back.focus();
}

/** The person has chosen to wait it out: do not raise it again this session. */
function dismissWaking() {
  wakingDismissed = true;
  closeWaking();
}

/**
 * Stop waiting on the cold start, however the waiting ended. The dismissal is
 * cleared with it: a later session — Start over opens one — gets to say that
 * it is slow on its own account.
 */
function doneWaking() {
  if (wakingTimer !== null) {
    clearTimeout(wakingTimer);
    wakingTimer = null;
  }
  wakingDismissed = false;
  closeWaking();
}

// --- feel ----------------------------------------------------------------

/**
 * Haptics (web-haptics, vendored; MIT). On Android this is the Vibration
 * API; on iOS Safari the library taps a hidden switch, which is the one thing
 * on that platform that produces a haptic from a page. Each moment gets the
 * library's preset closest to what it means: a light tap acknowledges a
 * touch, "success" is the session becoming real, "selection" is the first
 * word of a reply, "warning" and "error" are what they say, and End is a
 * single firm tick. Intensity sits under the library's default: the brand is
 * calm, not buzzy.
 *
 * On by default, remembered on this phone, and switchable on the landing.
 * Off means nothing is ever triggered. If the library fails to load, the
 * page is simply silent to the hand.
 */
const HAPTICS_KEY = 'gnsis.haptics';
const MODEL_HAPTIC_PRESETS = Object.freeze({
  attention: 'medium',
  proximity: 'selection',
  confirmation: 'success',
  warning: 'warning',
});

const haptics = {
  engine: null,
  intensity: 0.55,
  get enabled() {
    try { return localStorage.getItem(HAPTICS_KEY) !== 'off'; } catch { return true; }
  },
  set enabled(on) {
    try { localStorage.setItem(HAPTICS_KEY, on ? 'on' : 'off'); } catch { /* not remembered */ }
  },
  async load() {
    try {
      const mod = await import('/assets/live/web-haptics.mjs');
      this.engine = new mod.WebHaptics();
    } catch {
      this.engine = null;
    }
  },
  play(preset) {
    if (!this.engine || !this.enabled) return;
    try { void this.engine.trigger(preset, { intensity: this.intensity }); } catch { /* nothing */ }
  },
};

/** The status line morphs between messages (torph, vendored; MIT). */
let morph = null;
async function loadMorph() {
  try {
    const mod = await import('/assets/live/torph.mjs');
    morph = new mod.TextMorph({
      element: ui.status,
      text: ui.status.textContent || '',
      duration: 250,
      ease: 'cubic-bezier(0.4, 0, 0.2, 1)',
      respectReducedMotion: true,
    });
  } catch {
    morph = null;
  }
}

// --- headline ------------------------------------------------------------

/**
 * Bring the headline in one word at a time (Spell UI's WordsStagger, ported).
 *
 * The sentence is taken from the markup, so it is written once and a page
 * whose script never ran still shows it. Words are wrapped in spans separated
 * by real spaces — not flex items with a non-breaking space inside — so the
 * line wraps and centres as text and assistive tech reads one sentence.
 *
 * The animation is CSS, so the page's reduced-motion rule covers it. The
 * `stagger` class comes off when the last word lands: the landing is hidden
 * with display:none while the camera is up, and a CSS animation replays every
 * time an element returns from that, which would make Not now a light show.
 */
function staggerWords(heading) {
  if (!heading) return;
  const words = heading.textContent.split(' ').filter((word) => word.length > 0);
  if (words.length === 0) return;
  heading.textContent = '';
  words.forEach((word, index) => {
    const span = document.createElement('span');
    span.className = 'word';
    span.style.setProperty('--i', String(index));
    span.textContent = word;
    heading.append(span);
    if (index < words.length - 1) heading.append(' ');
  });
  heading.classList.add('stagger');
  const last = heading.lastElementChild;
  last.addEventListener('animationend', () => heading.classList.remove('stagger'), { once: true });
}

staggerWords(document.getElementById('headline'));

/**
 * Let the mark breathe.
 *
 * It is served as a picture so a page whose script never ran still shows it.
 * Here the same file is fetched and put inline, which is what lets CSS reach
 * the body and the eyes for the idle and attention motion the guideline
 * describes. If the fetch fails the picture simply stays.
 */
async function inlineMark(img, suffix = '') {
  if (!img) return;
  try {
    const response = await fetch(img.getAttribute('src'));
    if (!response.ok) return;
    let text = await response.text();
    // The same file goes inline twice (landing header and camera stage), and
    // its gradients and clip are found by id. Two copies would share ids, and
    // the header copy is display:none during a session — a gradient inside a
    // hidden SVG does not paint, so the stage copy would lose its colour.
    // Each copy gets ids of its own.
    if (suffix) {
      const ids = [...text.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
      for (const id of ids) {
        text = text
          .replaceAll(`id="${id}"`, `id="${id}${suffix}"`)
          .replaceAll(`url(#${id})`, `url(#${id}${suffix})`)
          .replaceAll(`"#${id}"`, `"#${id}${suffix}"`);
      }
    }
    const doc = new DOMParser().parseFromString(text, 'image/svg+xml');
    const svg = doc.documentElement;
    if (!svg || svg.nodeName !== 'svg') return;
    svg.setAttribute('class', 'mark');
    svg.setAttribute('id', img.id);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', img.alt || 'gnsis');
    img.replaceWith(svg);
  } catch {
    // The picture stays.
  }
}

void inlineMark(document.getElementById('mark'));
void inlineMark(document.getElementById('stageMark'), '-stage');

if (ui.hapticsSwitch) {
  // Where the browser knows what a switch is (Safari 17.4+), it draws it and
  // gives its own haptic on toggle; the page's drawing is for everywhere else.
  if ('switch' in ui.hapticsSwitch) ui.hapticsSwitch.classList.add('native');
  ui.hapticsSwitch.checked = haptics.enabled;
  ui.hapticsSwitch.addEventListener('change', () => {
    haptics.enabled = ui.hapticsSwitch.checked;
    // Turning it on says so, once, in the hand.
    if (ui.hapticsSwitch.checked) haptics.play('selection');
  });
}
void haptics.load();
void loadMorph();

/** The line the status pill is showing now, so a late settle cannot hide a newer one. */
let currentStatus = '';

function show(text, { state, tone } = {}) {
  currentStatus = text || '';
  if (morph) morph.update(text || '');
  else ui.status.textContent = text;
  ui.statusText.textContent = text;
  ui.status.hidden = !text;
  if (state) ui.root.dataset.state = state;
  if (tone === null) delete ui.root.dataset.tone;
  else if (tone) ui.root.dataset.tone = tone;
}

/** Let a line that only confirms something step aside after a moment. */
function settle(text) {
  setTimeout(() => {
    if (live && currentStatus === text) show('');
  }, SETTLE_MS);
}

/** Empty the caption card, ready for the next reply. */
function clearSays() {
  ui.says.textContent = '';
  ui.says.hidden = true;
  ui.says.classList.remove('full');
}

/**
 * Add one piece of a reply to the caption card. Each piece is its own span so
 * it can arrive with the headline's motion without replaying what is already
 * there; the card keeps the newest line in view.
 */
function appendSays(text) {
  const bit = document.createElement('span');
  bit.className = 'bit';
  bit.textContent = text;
  ui.says.append(bit);
  ui.says.hidden = false;
  fitSays();
}

/**
 * Keep the newest line in view. The card is display:none while you are not
 * muted, and a hidden box has no size to scroll, so this runs again the
 * moment Mute shows it.
 */
function fitSays() {
  ui.says.scrollTop = ui.says.scrollHeight;
  ui.says.classList.toggle('full', ui.says.scrollHeight > ui.says.clientHeight + 1);
}

const themeColor = document.querySelector('meta[name="theme-color"]');

/** Switch views, and let the browser's own chrome match the one on screen. */
function setView(view) {
  ui.root.dataset.view = view;
  if (themeColor) themeColor.content = view === 'camera' ? '#F5F8FB' : '#FFF8F5';
}

// --- session controls ----------------------------------------------------

/**
 * The landing's line about how the last session ended. The status pill lives
 * on the camera view, so a session that ends in an error needs somewhere to
 * say so after the landing comes back.
 */
function note(text, tone) {
  ui.notice.textContent = text || '';
  ui.notice.hidden = !text;
  if (tone) ui.notice.dataset.tone = tone;
  else delete ui.notice.dataset.tone;
}

/**
 * Mute silences the track rather than stopping it: the duplex stream keeps
 * flowing as silence, the way a muted call does, so unmuting is instant.
 * While muted, what gnsis says is shown as captions.
 */
function setMuted(on) {
  if (live) {
    live.muted = on;
    for (const track of live.media.audio.getAudioTracks()) track.enabled = !on;
  }
  if (on) {
    ui.root.dataset.muted = 'true';
    fitSays();
  } else {
    delete ui.root.dataset.muted;
  }
  ui.mute.toggleAttribute('data-off', on);
  ui.muteLabel.textContent = on ? 'Unmute' : 'Mute';
}

/**
 * Keep the last frame for Camera off. Drawn small — it is shown blurred, so
 * detail would be wasted — and dropped again when the camera comes back or
 * the session ends, so no picture outlives the moment it is needed.
 */
function holdLastFrame() {
  const video = ui.preview;
  if (!video.videoWidth) return;
  const scale = Math.min(1, 320 / Math.max(video.videoWidth, video.videoHeight));
  ui.ghost.width = Math.max(2, Math.round(video.videoWidth * scale));
  ui.ghost.height = Math.max(2, Math.round(video.videoHeight * scale));
  ui.ghost.getContext('2d').drawImage(video, 0, 0, ui.ghost.width, ui.ghost.height);
}

function dropLastFrame() {
  ui.ghost.width = 0;
  ui.ghost.height = 0;
}

/**
 * What gnsis is looking at: 'camera', 'screen', or nothing. One source at a
 * time — the runtime takes one video source per session — so choosing one
 * replaces the other.
 */
function setSourceUi(kind) {
  ui.root.dataset.source = kind || 'none';
  const camera = kind === 'camera';
  ui.camToggle.setAttribute('aria-pressed', String(camera));
  ui.camToggle.toggleAttribute('data-off', !camera);
  const screen = kind === 'screen';
  ui.share.toggleAttribute('data-active', screen);
  ui.shareLabel.textContent = screen ? 'Stop sharing' : 'Share screen';
}

function setSwitchesDisabled(off) {
  ui.camToggle.disabled = off;
  ui.share.disabled = off;
}

/** Back to how a session starts: everything on, nothing in flight. */
function resetControls(kind = 'camera') {
  setMuted(false);
  setSourceUi(kind);
  ui.mute.disabled = true;
  ui.end.disabled = false;
  ui.share.hidden = !canShareScreen;
  // The camera and share switches wait for proof of sight: before the first
  // frame is accepted there is nothing to switch yet.
  setSwitchesDisabled(true);
}

resetControls();

// --- the voice light -----------------------------------------------------

/**
 * Feeds the voice light from the real audio: gnsis's playback level lifts it,
 * the microphone level brightens its rim. Levels rise fast and fall slowly,
 * because speech comes in bursts and a light that snaps off between words
 * reads as flicker. Under reduced motion the loop does not run and the light
 * simply brightens while gnsis is speaking (CSS, from `data-voice`).
 */
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
let meterFrame = null;

function startMeter(session) {
  if (reduceMotion.matches) return;
  let gnsis = 0;
  let you = 0;
  const step = () => {
    if (live !== session || session.stopped) { meterFrame = null; return; }
    const g = Math.min(1, session.playback.level() * 5);
    const y = session.muted ? 0 : Math.min(1, (session.micLevel || 0) * 8);
    gnsis += (g - gnsis) * (g > gnsis ? 0.35 : 0.08);
    you += (y - you) * (y > you ? 0.35 : 0.08);
    ui.voice.style.setProperty('--gnsis', gnsis.toFixed(3));
    ui.voice.style.setProperty('--you', you.toFixed(3));
    meterFrame = requestAnimationFrame(step);
  };
  meterFrame = requestAnimationFrame(step);
}

function stopMeter() {
  if (meterFrame !== null) cancelAnimationFrame(meterFrame);
  meterFrame = null;
  ui.voice.style.removeProperty('--gnsis');
  ui.voice.style.removeProperty('--you');
}

// --- audio ---------------------------------------------------------------

/** Linear resample to 16 kHz PCM16, which is what /ws/duplex expects. */
function toPcm16(samples, fromRate) {
  const ratio = fromRate / INPUT_RATE;
  const count = Math.max(1, Math.round(samples.length / ratio));
  const out = new Int16Array(count);
  for (let i = 0; i < count; i += 1) {
    const at = i * ratio;
    const low = Math.floor(at);
    const high = Math.min(low + 1, samples.length - 1);
    const value = samples[low] + (samples[high] - samples[low]) * (at - low);
    out[i] = Math.max(-32768, Math.min(32767, Math.round(value * 32767)));
  }
  return out.buffer;
}

/**
 * Schedules GNSIS's speech so consecutive packets do not overlap or gap, and
 * reports when speech starts and stops being audible and how loud it is —
 * which is what moves the voice light, so it only moves for a real voice.
 */
function createPlayback(onVoice = () => {}) {
  const context = new (window.AudioContext || window.webkitAudioContext)();
  const sources = new Set();
  let cursor = 0;
  // Everything played passes through one analyser on its way out, so the
  // voice light reads the level that is actually coming out of the speaker.
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  analyser.connect(context.destination);
  const window_ = new Float32Array(analyser.fftSize);
  return {
    context,
    /** Loudness of what is playing now, 0 to about 0.5 for speech. */
    level() {
      if (sources.size === 0) return 0;
      analyser.getFloatTimeDomainData(window_);
      let sum = 0;
      for (let i = 0; i < window_.length; i += 1) sum += window_[i] * window_[i];
      return Math.sqrt(sum / window_.length);
    },
    play(bytes, rate) {
      const pcm = new Int16Array(bytes);
      if (!pcm.length) return;
      const buffer = context.createBuffer(1, pcm.length, rate);
      const channel = buffer.getChannelData(0);
      for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768;
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(analyser);
      const at = Math.max(context.currentTime + 0.02, cursor);
      source.start(at);
      cursor = at + buffer.duration;
      sources.add(source);
      if (sources.size === 1) onVoice(true);
      source.onended = () => {
        sources.delete(source);
        if (sources.size === 0) onVoice(false);
      };
    },
    cancel() {
      for (const source of sources) {
        source.onended = null;
        try { source.stop(); } catch { /* already finished */ }
      }
      sources.clear();
      cursor = 0;
      onVoice(false);
    },
    async close() {
      this.cancel();
      try { await context.close(); } catch { /* already closed */ }
    },
  };
}

// --- session -------------------------------------------------------------

function socketUrl(path, query) {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}${path}${query ? `?${query}` : ''}`;
}

async function openCamera() {
  show('Requesting camera…', { state: 'connecting', tone: null });
  const video = await navigator.mediaDevices.getUserMedia({ video: VIDEO_CONSTRAINTS });
  show('Requesting microphone…');
  let audio;
  try {
    audio = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (error) {
    for (const track of video.getTracks()) track.stop();
    throw error;
  }
  return { video, audio };
}

/**
 * A shared tab or screen, then the microphone. The picker has to open inside
 * the click that asked for it, so nothing is awaited before it.
 */
async function openScreen() {
  const video = await navigator.mediaDevices.getDisplayMedia(SCREEN_OPTIONS);
  show('Requesting microphone…', { state: 'connecting', tone: null });
  let audio;
  try {
    audio = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (error) {
    for (const track of video.getTracks()) track.stop();
    throw error;
  }
  return { video, audio };
}

/**
 * Stopping a share from the browser's own bar ("Stop sharing"), or a camera
 * that disappears, ends the track without this page asking. Treat it as the
 * source being switched off, so the runtime is told and the screen says so.
 */
function watchSource(session, stream, kind) {
  const track = stream.getVideoTracks()[0];
  if (!track) return;
  track.addEventListener('ended', () => {
    if (live !== session || session.stopped || session.media.video !== stream) return;
    goDark(session, kind === 'screen' ? 'Screen sharing stopped.' : 'Camera off.');
  }, { once: true });
}

function startFrames(session, hints) {
  const canvas = ui.scratch;
  const context = canvas.getContext('2d', { alpha: false });
  const rate = Math.min(Math.max(Number(hints.recommended_frame_rate) || 2, 0.5), 10);
  let sequence = 0;
  let inFlight = false;

  const tick = async () => {
    if (!live || live.stopped || inFlight || !live.source || live.pendingSource) return;
    const video = ui.preview;
    if (!video.videoWidth) return;
    inFlight = true;
    try {
      const kind = live.source;
      const edge = kind === 'screen' ? SCREEN_MAX_EDGE : MAX_EDGE;
      const scale = Math.min(1, edge / Math.max(video.videoWidth, video.videoHeight));
      const width = Math.max(2, Math.round(video.videoWidth * scale));
      const height = Math.max(2, Math.round(video.videoHeight * scale));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      context.drawImage(video, 0, 0, width, height);
      const quality = kind === 'screen' ? SCREEN_JPEG_QUALITY : JPEG_QUALITY;
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
      if (!blob || !live || live.stopped) return;
      // A switch landed while this frame was encoding: it belongs to the old
      // source, so it is not sent under the new one's name.
      if (live.source !== kind || live.pendingSource) return;
      const screen = live.screen;
      if (!screen || screen.readyState !== WebSocket.OPEN) return;
      sequence += 1;
      screen.send(JSON.stringify({
        type: 'screen.frame',
        frame_id: `live-${sequence}`,
        captured_at_ms: Date.now(),
        encoding: 'jpeg',
        video_source: kind,
      }));
      screen.send(await blob.arrayBuffer());
      live.stats.sent += 1;
    } finally {
      inFlight = false;
    }
  };

  return setInterval(() => { void tick(); }, Math.round(1000 / rate));
}

async function startMicrophone(session) {
  const context = new (window.AudioContext || window.webkitAudioContext)();
  await context.audioWorklet.addModule('/assets/mic-worklet.js');
  const source = context.createMediaStreamSource(session.media.audio);
  const capture = new AudioWorkletNode(context, 'minicpm-mic-capture', {
    processorOptions: { frameSize: 2048 },
  });
  capture.port.onmessage = (event) => {
    if (!live || live.stopped) return;
    const duplex = live.duplex;
    if (!duplex || duplex.readyState !== WebSocket.OPEN) return;
    // Your level, for the voice light's rim. A muted track delivers silence,
    // so this reads zero while muted without a special case.
    const samples = event.data.samples;
    let sum = 0;
    for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
    live.micLevel = Math.sqrt(sum / Math.max(1, samples.length));
    duplex.send(toPcm16(samples, context.sampleRate));
    live.stats.audio += 1;
  };
  source.connect(capture);
  // Not connected to the destination: routing the microphone to the speaker
  // would feed GNSIS's own voice straight back into it.
  return { context, source, capture };
}

function attachScreen(session, ready) {
  const screen = ready.screen || {};
  if (!screen.enabled || !screen.token) {
    show('Video is off on this server.', { tone: 'busy' });
    return null;
  }
  const socket = new WebSocket(
    socketUrl(screen.path || '/ws/screen', `session_id=${encodeURIComponent(ready.session_id)}&token=${encodeURIComponent(screen.token)}`)
  );
  socket.binaryType = 'arraybuffer';
  socket.addEventListener('message', (event) => {
    // End sets `live` to null before this socket finishes closing, and a
    // frame acknowledgement can still land in that gap.
    if (!live || live.stopped || typeof event.data !== 'string') return;
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }
    if (payload.type === 'screen.ready') {
      if (live.frameTimer) clearInterval(live.frameTimer);
      live.frameTimer = startFrames(session, payload);
      return;
    }
    if (payload.type === 'screen.frame.accepted') {
      live.stats.accepted += 1;
      if (live.stats.accepted === 1) {
        // The first proof that a real frame arrived. Only now is it true.
        show('Session ready.', { state: 'live', tone: 'live' });
        haptics.play('success');
        setSwitchesDisabled(false);
        settle('Session ready.');
      } else if (live.awaitingSight) {
        // The same rule after a switch: seen, not just sent.
        live.awaitingSight = false;
        const line = live.source === 'screen' ? 'Sharing your screen.' : 'Camera on.';
        show(line, { tone: 'live' });
        settle(line);
      }
      return;
    }
    if (payload.type === 'screen.frame.dropped') live.stats.dropped += 1;
  });
  return socket;
}

function handleDuplex(session, event) {
  if (typeof event.data !== 'string') {
    // Binary always follows the audio.chunk header that describes it.
    const header = live.pendingAudio;
    live.pendingAudio = null;
    if (header) live.playback.play(event.data, header.audio_sample_rate || 24000);
    return;
  }
  let payload;
  try { payload = JSON.parse(event.data); } catch { return; }

  switch (payload.type) {
    case 'runtime.status':
      // Sent every few seconds while a cold machine loads the model. Before
      // this case existed the frames arrived and nothing read them, so the
      // page sat on one unchanging line for the whole load.
      if (payload.status === 'loading') wakingUp();
      return;

    case 'ready':
      doneWaking();
      live.sessionId = payload.session_id;
      live.ready = payload;
      // The session starts in voice mode, and /ws/screen refuses every frame
      // while it is ("media_mode is voice"). Opening the socket first looks
      // like it works and then sits on "Waiting for the first frame…" for
      // ever. So ask for camera mode and wait to be told yes.
      if (payload.screen && payload.screen.enabled) {
        live.duplex.send(JSON.stringify({
          type: 'media.mode', video: true, source: live.pendingSource,
        }));
        show(live.pendingSource === 'screen' ? 'Starting to share…' : 'Turning the camera on…', { state: 'connecting' });
      } else {
        show('Video is off on this server.', { tone: 'busy' });
      }
      void startMicrophone(session).then((mic) => { if (live) live.mic = mic; });
      break;
    case 'media.mode.done':
      // Back to voice after Camera off: nothing to attach, frames already
      // paused.
      if (payload.video === false) break;
      if (live.pendingSource) {
        live.source = live.pendingSource;
        live.pendingSource = null;
        // The very first frame has its own line ("Session ready."); after
        // that, a switch waits for its own proof.
        live.awaitingSight = live.stats.accepted > 0;
      }
      // Only now is the server willing to look at a frame.
      if ((!live.screen || live.screen.readyState > WebSocket.OPEN) && live.ready) {
        live.screen = attachScreen(session, live.ready);
        if (live.stats.accepted === 0) show('Waiting for the first frame…', { state: 'connecting' });
      }
      break;
    case 'media.mode.rejected': {
      const refused = live.pendingSource;
      if (refused) {
        // The switch was refused: let go of what we just opened.
        live.pendingSource = null;
        live.source = null;
        for (const track of live.media.video.getTracks()) track.stop();
        ui.preview.srcObject = null;
        setSourceUi(null);
      }
      show(refused === 'screen' ? 'This session cannot see your screen.' : 'This session cannot use the camera.', { tone: 'busy' });
      break;
    }
    case 'audio.chunk':
      live.pendingAudio = payload;
      break;
    case 'playback.cancel':
      live.playback.cancel();
      break;
    case 'haptic.cue': {
      // GNSIS chooses meaning, never a raw motor pattern. The device owns how
      // that meaning feels, which keeps the protocol stable across hardware.
      const preset = MODEL_HAPTIC_PRESETS[payload.cue];
      if (preset) {
        haptics.play(preset);
        live.lastModelHapticAt = performance.now();
      }
      break;
    }
    case 'chunk':
      // Where the model's words actually come from. `turn.final.accepted`
      // only acknowledges a transcript the client sent and carries no text,
      // so reading it left the page silent whenever speech was off.
      if (payload.text) {
        // A model-directed cue immediately before speech already supplied the
        // tactile intent. Do not stack the generic first-word tick on top.
        const justFeltModelCue = (
          live.lastModelHapticAt != null
          && performance.now() - live.lastModelHapticAt < 750
        );
        if (!live.reply && !justFeltModelCue) haptics.play('selection');
        // A new reply starts a clean card.
        if (!live.reply) clearSays();
        live.reply = (live.reply || '') + payload.text;
        appendSays(payload.text);
      }
      if (payload.end_of_turn || payload.interrupted) live.reply = '';
      break;
    case 'error':
      // Raw engineering wording never reaches the page.
      show(payload.fatal ? 'The session could not start.' : 'Something went wrong.', { tone: 'bad' });
      haptics.play('error');
      break;
    default:
      break;
  }
}

/** What the last session opened with, so Start over opens the same. */
let lastKind = 'camera';

async function start(kind = 'camera') {
  lastKind = kind;
  ui.start.disabled = true;
  clearSays();
  note('');
  let media;
  try {
    media = kind === 'screen' ? await openScreen() : await openCamera();
  } catch (error) {
    const denied = error && (error.name === 'NotAllowedError' || error.name === 'SecurityError');
    // Back to the landing, where the offer still stands — and where the
    // reason is now written, since the status pill is not on that view.
    setView('landing');
    show('', { state: 'idle', tone: null });
    if (kind === 'screen') {
      note(denied ? 'Nothing was shared, so no session started.' : 'This browser would not share the screen.', denied ? 'busy' : 'bad');
    } else {
      note(
        denied
          ? 'Camera or microphone access was not granted.'
          : 'This device would not start the camera.',
        'bad'
      );
    }
    ui.start.disabled = false;
    return;
  }

  ui.preview.srcObject = media.video;
  // One object for the whole session. The socket handlers below check
  // `live === session` to speak only for their own session; `session` used to
  // be a separate `{ media }` object, which `live` never equals, so a dropped
  // connection or a busy model was never reported and never released the
  // camera and microphone.
  live = {
    media,
    duplex: null,
    screen: null,
    mic: null,
    playback: createPlayback((speaking) => {
      if (speaking) ui.root.dataset.voice = 'speaking';
      else delete ui.root.dataset.voice;
    }),
    frameTimer: null,
    pendingAudio: null,
    sessionId: null,
    lastModelHapticAt: null,
    muted: false,
    micLevel: 0,
    // What frames are being sent from, and what a switch is waiting on.
    source: null,
    pendingSource: kind,
    awaitingSight: false,
    stopped: false,
    stats: { sent: 0, accepted: 0, dropped: 0, audio: 0 },
  };
  const session = live;
  watchSource(session, media.video, kind);

  setView('camera');
  resetControls(kind);
  ui.mute.disabled = false;
  startMeter(live);
  show('Connecting…', { state: 'connecting' });

  const duplex = new WebSocket(socketUrl('/ws/duplex'));
  duplex.binaryType = 'arraybuffer';
  live.duplex = duplex;
  duplex.addEventListener('message', (event) => handleDuplex(session, event));
  duplex.addEventListener('close', (event) => {
    // Only ever speak for the session this socket belonged to. Start over
    // closes this socket and opens another straight away, and a close
    // handshake can easily outlast that: without this check the old socket's
    // close would report "Connection lost" about the session that replaced
    // it, and then tear that session down. Clearing the cold-start notice is
    // inside the guard for the same reason — by then it is the new one's.
    if (live !== session || live.stopped) return;
    doneWaking();
    if (event.code === 1013) {
      // The single model slot is taken. Say that in words a person can act on.
      show('Another session is open. Try again in a moment.', { tone: 'busy' });
      haptics.play('warning');
    } else {
      show('Connection lost.', { tone: 'bad' });
      haptics.play('error');
    }
    void stop({ keepMessage: true });
  });
  duplex.addEventListener('error', () => {
    if (live === session && !live.stopped) show('Could not connect.', { tone: 'bad' });
  });
}

/**
 * Turn the picture off without a switch to anything else: hold the last
 * frame for the 2% trace, stop the track (the camera light or the browser's
 * "sharing" bar goes away), ask the runtime for voice, and say so.
 */
function goDark(session, message) {
  holdLastFrame();
  for (const track of session.media.video.getTracks()) track.stop();
  ui.preview.srcObject = null;
  session.source = null;
  session.pendingSource = null;
  session.awaitingSight = false;
  setSourceUi(null);
  const duplex = session.duplex;
  if (duplex && duplex.readyState === WebSocket.OPEN) {
    duplex.send(JSON.stringify({ type: 'media.mode', video: false }));
  }
  show(message, { tone: null });
}

/**
 * The camera and share switches. `kind` is what gnsis should look at next:
 * 'camera', 'screen', or null for nothing. A new source is opened first and
 * only then replaces the old one, so a cancelled picker leaves everything as
 * it was. The picture is only called live once a frame from the new source
 * has been accepted, the same rule the session started under.
 */
async function setSource(kind) {
  const session = live;
  if (!session || session.stopped || session.sourceBusy) return;
  const duplex = session.duplex;
  if (!duplex || duplex.readyState !== WebSocket.OPEN) return;
  const current = session.pendingSource || session.source;
  if (kind === current) return;
  if (!kind) {
    goDark(session, current === 'screen' ? 'Screen sharing stopped.' : 'Camera off.');
    return;
  }
  session.sourceBusy = true;
  setSwitchesDisabled(true);
  try {
    let stream;
    try {
      if (kind === 'screen') {
        // Opened straight from the click, before anything is awaited.
        stream = await navigator.mediaDevices.getDisplayMedia(SCREEN_OPTIONS);
      } else {
        show('Turning the camera on…', { tone: 'busy' });
        stream = await navigator.mediaDevices.getUserMedia({ video: VIDEO_CONSTRAINTS });
      }
    } catch (error) {
      // Closing the picker is a choice, not a failure: nothing changes.
      if (kind === 'screen' && error && error.name === 'NotAllowedError') return;
      show(kind === 'screen' ? 'This browser would not share the screen.' : 'This device would not start the camera.', { tone: 'bad' });
      return;
    }
    // End may have been pressed while the picker or the lens was open.
    if (live !== session || session.stopped) {
      for (const track of stream.getTracks()) track.stop();
      return;
    }
    // Hand over: the old source stops only now that the new one exists.
    for (const track of session.media.video.getTracks()) track.stop();
    session.media.video = stream;
    watchSource(session, stream, kind);
    ui.preview.srcObject = stream;
    dropLastFrame();
    session.source = null;
    session.pendingSource = kind;
    session.awaitingSight = false;
    setSourceUi(kind);
    if (kind === 'screen') show('Starting to share…', { tone: 'busy' });
    duplex.send(JSON.stringify({ type: 'media.mode', video: true, source: kind }));
  } finally {
    session.sourceBusy = false;
    if (live === session) setSwitchesDisabled(false);
  }
}

/**
 * `ended` is End pressed on purpose: the stage shows the session closing —
 * picture out, light down, gnsis's eyes shut, "Session ended." — while the
 * runtime is told to stop, then the landing comes back. `keepMessage` is an
 * ending that was not chosen (connection lost, busy, a failed start): no
 * farewell, and the reason is written on the landing.
 */
async function stop({ keepMessage = false, ended = false } = {}) {
  doneWaking();
  const session = live;
  if (!session || session.stopped) return;
  session.stopped = true;
  live = null;
  stopMeter();

  const tone = ui.root.dataset.tone;
  const leaving = keepMessage && (tone === 'bad' || tone === 'busy') ? { text: currentStatus, tone } : null;
  if (ended) {
    ui.root.dataset.state = 'ending';
    ui.end.disabled = true;
    ui.mute.disabled = true;
    ui.camToggle.disabled = true;
    show('Ending…', { tone: null });
  }

  if (session.frameTimer) clearInterval(session.frameTimer);

  // Say stop, and wait to be told the session is done.
  //
  // Closing the socket outright reads to the server as a dropped connection,
  // which parks the Thinker and holds the single model slot for the whole
  // reconnect grace. End, then scan again, and the next person is told it
  // is busy — by a session the last person deliberately ended. An explicit
  // stop is never parked.
  const duplex = session.duplex;
  if (duplex && duplex.readyState === WebSocket.OPEN) {
    await new Promise((resolve) => {
      // The wait is bounded: a server that never answers must not strand the
      // page with the camera still on.
      const giveUp = setTimeout(resolve, 1500);
      duplex.addEventListener('message', function done(event) {
        if (typeof event.data !== 'string') return;
        try {
          if (JSON.parse(event.data).type !== 'session.done') return;
        } catch { return; }
        duplex.removeEventListener('message', done);
        clearTimeout(giveUp);
        resolve();
      });
      try { duplex.send(JSON.stringify({ type: 'stop' })); } catch { resolve(); }
    });
  }

  for (const socket of [session.screen, session.duplex]) {
    if (socket && socket.readyState <= WebSocket.OPEN) {
      try { socket.close(1000, 'ended'); } catch { /* already closing */ }
    }
  }
  if (session.mic) {
    session.mic.capture.port.onmessage = null;
    try { session.mic.source.disconnect(); } catch { /* not connected */ }
    try { session.mic.capture.disconnect(); } catch { /* not connected */ }
    try { await session.mic.context.close(); } catch { /* already closed */ }
  }
  await session.playback.close();
  // Last, and unconditionally: this is what turns the phone's indicators off.
  for (const stream of [session.media.video, session.media.audio]) {
    for (const track of stream.getTracks()) track.stop();
  }
  ui.preview.srcObject = null;

  if (ended) {
    // Everything is off by now; this is only the moment of saying so.
    show('Session ended.');
    await new Promise((resolve) => { setTimeout(resolve, ENDED_HOLD_MS); });
  }

  clearSays();
  resetControls();
  dropLastFrame();
  delete ui.root.dataset.voice;
  show('', { state: 'idle', tone: null });
  setView('landing');
  ui.start.disabled = false;
  note(leaving ? leaving.text : '', leaving ? leaving.tone : null);
}

/**
 * Ask in our own words before the browser asks in its own.
 *
 * `getUserMedia` is deliberately not called here. On a phone a dismissed
 * native prompt is awkward to recover from — on iOS it means digging through
 * Settings — so the sheet explains what is about to be asked while saying no
 * still costs nothing. The native prompt fires on Allow, and only then.
 */
const ASKS = Object.freeze({
  camera: {
    title: 'Camera and microphone',
    body: 'Point the camera at something and talk normally.',
    allow: 'Allow camera and microphone',
  },
  screen: {
    title: 'Screen and microphone',
    body: 'Pick a tab or your whole screen, then talk normally.',
    allow: 'Choose what to share',
  },
});

/** What the open sheet is asking for, so Allow starts the right session. */
let asking = 'camera';

function ask(kind = 'camera') {
  asking = kind;
  ui.askTitle.textContent = ASKS[kind].title;
  ui.askBody.textContent = ASKS[kind].body;
  ui.allow.textContent = ASKS[kind].allow;
  setView('camera');
  ui.permission.hidden = false;
  ui.allow.focus({ preventScroll: true });
}

function dismiss() {
  ui.permission.hidden = true;
  setView('landing');
}

ui.start.addEventListener('click', () => { haptics.play('light'); ask(); });
// A desktop with a webcam is not shut out; it just is not the default door.
if (ui.useThis) {
  ui.useThis.addEventListener('click', () => {
    ui.root.dataset.force = 'touch';
    ask('camera');
  });
}
if (ui.useScreen && canShareScreen) {
  ui.useScreen.hidden = false;
  ui.useScreen.addEventListener('click', () => ask('screen'));
}
ui.allow.addEventListener('click', () => {
  haptics.play('light');
  ui.permission.hidden = true;
  void start(asking);
});
ui.cancel.addEventListener('click', dismiss);
// Keep waiting only dismisses the notice: the wait was never interrupted, so
// there is nothing to resume. End is behind this, and reachable again once it
// is gone.
ui.wakingWait.addEventListener('click', dismissWaking);

// Start over does NOT make the model load faster — it is already loading, and
// a fresh socket joins the same wait. It is here for the case where the wait
// is not the model at all: a socket that died quietly, a phone that slept.
ui.wakingRetry.addEventListener('click', async () => {
  closeWaking();
  await stop({ keepMessage: true });
  await start(lastKind);
});

ui.permission.addEventListener('click', (event) => {
  // Tapping the dimmed area is a refusal too.
  if (event.target === ui.permission) dismiss();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !ui.permission.hidden) dismiss();
  else if (event.key === 'Escape' && !ui.waking.hidden) dismissWaking();
});

ui.end.addEventListener('click', () => { haptics.play('rigid'); void stop({ ended: true }); });
ui.mute.addEventListener('click', () => {
  if (!live) return;
  haptics.play('light');
  setMuted(!live.muted);
});
ui.camToggle.addEventListener('click', () => {
  if (!live) return;
  haptics.play('light');
  const current = live.pendingSource || live.source;
  void setSource(current === 'camera' ? null : 'camera');
});
ui.share.addEventListener('click', () => {
  if (!live) return;
  const current = live.pendingSource || live.source;
  // Synchronous down to getDisplayMedia, so the browser sees the click.
  void setSource(current === 'screen' ? null : 'screen');
});

// A backgrounded or closed tab must not leave the camera on. (Not fired by
// switching to another tab while sharing: `pagehide` is the page going away,
// not going out of view.)
window.addEventListener('pagehide', () => { void stop({ keepMessage: true }); });
