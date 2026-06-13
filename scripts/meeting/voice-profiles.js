"use strict";
/**
 * Voice profile store + matching (Node).
 * Profiles live ONLY on this machine (~/.claude-daemon/voice-profiles.json).
 * Embeddings are produced by speaker_embed.py (onnx wespeaker, no PyTorch).
 *
 * Validated separation (real Korean meeting): same-speaker ~0.62,
 * different-speaker ~0.11. Threshold 0.35 sits safely between.
 */

const fs = require("fs");
const path = require("path");
const os = require("os");
const { spawnSync } = require("child_process");

const MATCH_THRESHOLD = parseFloat(process.env.VOICE_MATCH_THRESHOLD || "0.35");

function profilesPath(configDir) {
  return path.join(configDir || path.join(os.homedir(), ".claude-daemon"), "voice-profiles.json");
}

function loadProfiles(configDir) {
  try {
    return JSON.parse(fs.readFileSync(profilesPath(configDir), "utf-8"));
  } catch {
    return [];
  }
}

function saveProfiles(configDir, profiles) {
  fs.writeFileSync(profilesPath(configDir), JSON.stringify(profiles, null, 2));
}

function cosine(a, b) {
  let dot = 0;
  for (let i = 0; i < a.length; i++) dot += a[i] * b[i];
  return dot; // both are L2-normalized
}

/** Best matching profile name for an embedding, or null. */
function matchSpeaker(emb, profiles, threshold = MATCH_THRESHOLD) {
  let best = null, bestScore = -1;
  for (const p of profiles) {
    if (!Array.isArray(p.embedding)) continue;
    const s = cosine(emb, p.embedding);
    if (s > bestScore) { bestScore = s; best = p; }
  }
  if (best && bestScore >= threshold) return { name: best.name, score: bestScore };
  return null;
}

/** Enroll/merge an embedding under `name` (running average, weighted by sample count). */
function enroll(configDir, name, emb) {
  const profiles = loadProfiles(configDir);
  const existing = profiles.find((p) => p.name === name);
  if (existing && Array.isArray(existing.embedding)) {
    const n = existing.sample_count || 1;
    const merged = existing.embedding.map((v, i) => (v * n + emb[i]) / (n + 1));
    // renormalize
    const norm = Math.sqrt(merged.reduce((s, v) => s + v * v, 0)) || 1;
    existing.embedding = merged.map((v) => v / norm);
    existing.sample_count = n + 1;
    existing.updated_at = new Date().toISOString();
  } else {
    profiles.push({ name, embedding: emb, sample_count: 1, updated_at: new Date().toISOString() });
  }
  saveProfiles(configDir, profiles);
  return profiles;
}

/**
 * Run speaker_embed.py to get one averaged embedding per speaker.
 * segments: [{speaker, start_ms, end_ms}]. Returns { embeddings, durations }.
 */
function embedSpeakers(audioPath, segments, minDurSec = 3) {
  const py = path.join(path.dirname(__filename), "speaker_embed.py");
  const r = spawnSync("python3", [py], {
    input: JSON.stringify({ audio: audioPath, segments, min_dur_sec: minDurSec }),
    encoding: "utf-8",
    maxBuffer: 32 * 1024 * 1024,
    timeout: 5 * 60 * 1000,
  });
  if (r.status !== 0) {
    throw new Error(`speaker_embed failed: ${(r.stderr || "").slice(0, 300)}`);
  }
  const out = JSON.parse(r.stdout);
  if (!out.ok) throw new Error(`speaker_embed: ${out.error}`);
  return out;
}

module.exports = {
  MATCH_THRESHOLD,
  loadProfiles,
  saveProfiles,
  matchSpeaker,
  enroll,
  embedSpeakers,
  profilesPath,
};
