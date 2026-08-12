"use strict";
/**
 * Soniox async (file) transcription client (Node, no deps).
 * Uploads a local audio file, runs diarized transcription, polls to completion,
 * returns tokens, and cleans up remote artifacts.
 *
 * Flow: POST /v1/files → POST /v1/transcriptions → poll GET → GET .../transcript
 * Docs: https://soniox.com/docs/stt/async/async-transcription
 */

const fs = require("fs");
const path = require("path");
const os = require("os");
const { spawn } = require("child_process");

const API_BASE = "https://api.soniox.com";
// Async diarization model. If Soniox changes the id, update here only.
const ASYNC_MODEL = process.env.SONIOX_ASYNC_MODEL || "stt-async-v5";

function authHeaders(apiKey, extra = {}) {
  return { Authorization: `Bearer ${apiKey}`, ...extra };
}

async function soniox(apiKey, method, urlPath, body, isJson = true) {
  const res = await fetch(`${API_BASE}${urlPath}`, {
    method,
    headers: authHeaders(apiKey, isJson && body ? { "Content-Type": "application/json" } : {}),
    body: body ? (isJson ? JSON.stringify(body) : body) : undefined,
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Soniox ${method} ${urlPath} → ${res.status}: ${txt.slice(0, 300)}`);
  }
  return res.json().catch(() => ({}));
}

function findFfmpeg() {
  const candidates = [
    process.env.FFMPEG_PATH,
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
  ].filter(Boolean);
  for (const c of candidates) {
    try { if (fs.existsSync(c)) return c; } catch { /* ignore */ }
  }
  return "ffmpeg"; // rely on PATH
}

/** Run a command detached from the event loop; resolves the exit code (-1 on
 *  spawn error or timeout kill). spawnSync would freeze the WHOLE home portal
 *  (every request, every tenant on a shared host) for the remux duration. */
function run(cmd, cmdArgs, timeoutMs) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(cmd, cmdArgs, { stdio: "ignore" });
    } catch {
      return resolve(-1);
    }
    const timer = setTimeout(() => { try { child.kill("SIGKILL"); } catch { /* ignore */ } }, timeoutMs);
    child.on("error", () => { clearTimeout(timer); resolve(-1); });
    child.on("exit", (code) => { clearTimeout(timer); resolve(code == null ? -1 : code); });
  });
}

/**
 * Browser MediaRecorder WebM has no duration in its header (live-stream format),
 * so Soniox async rejects it with "Error determining audio duration". A fast
 * container-only remux (`-c copy`, no re-encode) rewrites a clean WebM with a
 * proper duration — stays compressed (no giant WAV), so the upload + memory
 * footprint stay small even for multi-hour meetings.
 * Returns the path/mime to upload (remuxed on success, original on failure).
 */
async function remuxForSoniox(filePath) {
  const ffmpeg = findFfmpeg();
  const outPath = path.join(os.tmpdir(), `pv-meeting-${path.basename(filePath, path.extname(filePath))}.remux.webm`);
  try {
    const code = await run(ffmpeg, ["-y", "-i", filePath, "-c", "copy", outPath], 5 * 60 * 1000);
    if (code === 0 && fs.existsSync(outPath) && fs.statSync(outPath).size > 0) {
      return { path: outPath, mime: "audio/webm", cleanup: true };
    }
  } catch { /* fall through */ }
  return { path: filePath, mime: "audio/webm", cleanup: false };
}

/**
 * Upload a local file to Soniox → returns file id.
 * Uses fs.openAsBlob so the file streams from disk instead of being read fully
 * into memory (a 3h recording would otherwise be a ~170MB Buffer).
 */
async function uploadFile(apiKey, filePath, mime = "audio/webm") {
  const blob = await fs.openAsBlob(filePath, { type: mime });
  const form = new FormData();
  form.append("file", blob, path.basename(filePath));
  const res = await fetch(`${API_BASE}/v1/files`, {
    method: "POST",
    headers: authHeaders(apiKey),
    body: form,
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Soniox upload → ${res.status}: ${txt.slice(0, 300)}`);
  }
  const data = await res.json();
  return data.id || data.file_id;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Transcribe a local audio file with speaker diarization.
 * opts: { pollIntervalMs, maxWaitMs, audioDurationSec, onProgress }
 * Returns { tokens, fileId, transcriptionId }.
 */
async function transcribeFile(apiKey, filePath, opts = {}) {
  const pollIntervalMs = opts.pollIntervalMs || 5000;
  // Scale the ceiling with audio length (async is faster than realtime, but a
  // 3h meeting still needs headroom). Floor 30 min, ~2× audio length otherwise.
  const maxWaitMs = opts.maxWaitMs
    || Math.max(30 * 60 * 1000, (opts.audioDurationSec || 0) * 1000 * 2);
  const onProgress = opts.onProgress || (() => {});

  let fileId, transcriptionId;
  let remuxed = null;
  try {
    onProgress("remuxing");
    remuxed = await remuxForSoniox(filePath);

    onProgress("uploading");
    fileId = await uploadFile(apiKey, remuxed.path, remuxed.mime);

    onProgress("submitting");
    const tr = await soniox(apiKey, "POST", "/v1/transcriptions", {
      file_id: fileId,
      model: ASYNC_MODEL,
      enable_speaker_diarization: true,
      // language auto-detected (multilingual model, no Korean penalty)
    });
    transcriptionId = tr.id;

    onProgress("processing");
    const deadline = Date.now() + maxWaitMs;
    // Long meetings poll for many minutes; a transient network outage (router
    // reboot, tunnel restart, wifi blip) must NOT kill a transcription Soniox
    // is still processing on its side. Tolerate poll errors for up to 10 min
    // since the last successful poll before giving up — a 3h meeting's precise
    // transcript is too valuable to abandon over a 30s blip.
    const POLL_ERROR_GRACE_MS = 10 * 60 * 1000;
    let lastPollOkAt = Date.now();
    let polls = 0;
    for (;;) {
      // Heartbeat every ~2 min so the meeting's updated_at stays fresh during a
      // long Soniox wait — the stuck-meeting sweep uses staleness to detect
      // orphans and must not mistake an actively-processing meeting for one.
      if (polls > 0 && polls % 24 === 0) onProgress("processing");
      polls++;
      let st;
      try {
        st = await soniox(apiKey, "GET", `/v1/transcriptions/${transcriptionId}`);
        lastPollOkAt = Date.now();
      } catch (e) {
        if (Date.now() - lastPollOkAt > POLL_ERROR_GRACE_MS) throw e;
        if (Date.now() > deadline) throw new Error("transcription timed out");
        await sleep(pollIntervalMs);
        continue;
      }
      const status = st.status;
      if (status === "completed") break;
      if (status === "error" || status === "failed") {
        throw new Error(`transcription failed: ${st.error_message || status}`);
      }
      if (Date.now() > deadline) throw new Error("transcription timed out");
      await sleep(pollIntervalMs);
    }

    onProgress("fetching");
    const result = await soniox(apiKey, "GET", `/v1/transcriptions/${transcriptionId}/transcript`);
    const tokens = result.tokens || [];
    return { tokens, fileId, transcriptionId };
  } finally {
    // Best-effort cleanup so we don't accumulate against Soniox quotas / keep PII
    if (transcriptionId) {
      soniox(apiKey, "DELETE", `/v1/transcriptions/${transcriptionId}`).catch(() => {});
    }
    if (fileId) {
      soniox(apiKey, "DELETE", `/v1/files/${fileId}`).catch(() => {});
    }
    if (remuxed && remuxed.cleanup) {
      try { fs.unlinkSync(remuxed.path); } catch { /* ignore */ }
    }
  }
}

module.exports = { transcribeFile, ASYNC_MODEL };
