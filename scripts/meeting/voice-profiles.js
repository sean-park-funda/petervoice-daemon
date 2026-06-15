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

/**
 * Ensure a profile has a `sources` list (lazy migration of legacy profiles
 * that only carried a single running-average `embedding`). Mutates in place.
 */
function ensureSources(profile) {
  if (Array.isArray(profile.sources)) return profile;
  profile.sources = Array.isArray(profile.embedding)
    ? [{ key: `legacy:${profile.name}`, embedding: profile.embedding, at: profile.updated_at || null }]
    : [];
  return profile;
}

/**
 * Recompute the derived `embedding` (L2-normalized mean of all source
 * embeddings) and `sample_count` from a profile's `sources`. Mutates in place.
 * Returns the profile, or null if it has no usable sources (caller drops it).
 */
function recompute(profile) {
  const srcs = (profile.sources || []).filter((s) => Array.isArray(s.embedding));
  if (srcs.length === 0) return null;
  const dim = srcs[0].embedding.length;
  const sum = new Array(dim).fill(0);
  for (const s of srcs) for (let i = 0; i < dim; i++) sum[i] += s.embedding[i];
  const mean = sum.map((v) => v / srcs.length);
  const norm = Math.sqrt(mean.reduce((acc, v) => acc + v * v, 0)) || 1;
  profile.embedding = mean.map((v) => v / norm);
  profile.sample_count = srcs.length;
  profile.updated_at = new Date().toISOString();
  return profile;
}

/**
 * Enroll an embedding under `name`, tracked by `sourceKey` (= `meetingId:speaker`).
 * Idempotent per source: re-enrolling the same key replaces that contribution
 * instead of double-counting. The derived embedding is recomputed from sources.
 * If `sourceKey` is omitted, falls back to an anonymous one-off source (legacy
 * callers) — but prefer always passing a stable key.
 */
function enroll(configDir, name, emb, sourceKey) {
  const profiles = loadProfiles(configDir);
  let existing = profiles.find((p) => p.name === name);
  if (!existing) {
    existing = { name, sources: [] };
    profiles.push(existing);
  }
  ensureSources(existing);
  const key = sourceKey || `oneoff:${name}:${existing.sources.length}`;
  existing.sources = existing.sources.filter((s) => s.key !== key);
  existing.sources.push({ key, embedding: emb, at: new Date().toISOString() });
  recompute(existing);
  saveProfiles(configDir, profiles);
  return profiles;
}

/**
 * Remove the contribution identified by `sourceKey` from `name`'s profile and
 * recompute. If the profile has no sources left, it is deleted entirely.
 * No-op if the profile or source does not exist.
 */
function unenroll(configDir, name, sourceKey) {
  const profiles = loadProfiles(configDir);
  const existing = profiles.find((p) => p.name === name);
  if (!existing) return profiles;
  ensureSources(existing);
  const before = existing.sources.length;
  existing.sources = existing.sources.filter((s) => s.key !== sourceKey);
  if (existing.sources.length === before) return profiles; // nothing removed
  if (!recompute(existing)) {
    // no usable sources remain → drop the profile
    const idx = profiles.indexOf(existing);
    if (idx >= 0) profiles.splice(idx, 1);
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
  unenroll,
  embedSpeakers,
  profilesPath,
};
