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

/** Upload local file via multipart → returns file id. */
async function uploadFile(apiKey, filePath) {
  const buf = fs.readFileSync(filePath);
  const form = new FormData();
  // Node 18+ Blob/FormData are global
  const blob = new Blob([buf], { type: "audio/webm" });
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
 * opts: { pollIntervalMs, maxWaitMs, onProgress }
 * Returns { tokens, fileId, transcriptionId }.
 */
async function transcribeFile(apiKey, filePath, opts = {}) {
  const pollIntervalMs = opts.pollIntervalMs || 5000;
  const maxWaitMs = opts.maxWaitMs || 30 * 60 * 1000; // 30 min ceiling
  const onProgress = opts.onProgress || (() => {});

  let fileId, transcriptionId;
  try {
    onProgress("uploading");
    fileId = await uploadFile(apiKey, filePath);

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
    for (;;) {
      const st = await soniox(apiKey, "GET", `/v1/transcriptions/${transcriptionId}`);
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
  }
}

module.exports = { transcribeFile, ASYNC_MODEL };
