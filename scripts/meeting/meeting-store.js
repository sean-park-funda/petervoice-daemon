"use strict";
/**
 * Meeting persistence (Node).
 * Audio + metadata live ONLY on this machine (~/.claude-daemon/meetings/).
 * The diarized transcript doc is written into the project's docs/meetings/.
 */

const fs = require("fs");
const path = require("path");
const os = require("os");

const { groupBySpeaker, buildTranscriptMarkdown, distinctSpeakers, speakerLabel } = require("./transcript");

function meetingsDir(configDir) {
  const dir = path.join(configDir || path.join(os.homedir(), ".claude-daemon"), "meetings");
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

const audioPath = (configDir, id) => path.join(meetingsDir(configDir), `${id}.webm`);
const metaPath = (configDir, id) => path.join(meetingsDir(configDir), `${id}.json`);

function readMeta(configDir, id) {
  try {
    return JSON.parse(fs.readFileSync(metaPath(configDir, id), "utf-8"));
  } catch {
    return null;
  }
}

function writeMeta(configDir, id, meta) {
  fs.writeFileSync(metaPath(configDir, id), JSON.stringify(meta, null, 2));
  return meta;
}

function updateMeta(configDir, id, patch) {
  const cur = readMeta(configDir, id) || {};
  return writeMeta(configDir, id, { ...cur, ...patch, updated_at: new Date().toISOString() });
}

/** Move an uploaded temp file into permanent local storage. */
function storeAudio(configDir, id, tempPath) {
  const dest = audioPath(configDir, id);
  fs.renameSync(tempPath, dest);
  return dest;
}

/** Delete the stored audio file (best-effort; called after transcription). */
function deleteAudio(configDir, id) {
  try { fs.unlinkSync(audioPath(configDir, id)); return true; } catch { return false; }
}

/** List meetings newest-first (metadata only). */
function listMeetings(configDir) {
  const dir = meetingsDir(configDir);
  return fs
    .readdirSync(dir)
    .filter((f) => f.endsWith(".json"))
    .map((f) => readMeta(configDir, f.replace(/\.json$/, "")))
    .filter(Boolean)
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
}

/** YYYY-MM-DD-HHMM from an ISO string (local-ish, no deps). */
function stampFromIso(iso) {
  const d = new Date(iso || Date.now());
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
}

function slugify(title) {
  return String(title || "meeting")
    .trim()
    .replace(/[\\/:*?"<>|]+/g, "")
    .replace(/\s+/g, "-")
    .slice(0, 40) || "meeting";
}

/**
 * Build the transcript doc from Soniox tokens and write it to the project's
 * docs/meetings/ folder. Returns { docPath, segments, speakers }.
 *
 * projectDocsDir: absolute path to the project's docs/ directory.
 */
function writeTranscriptDoc(projectDocsDir, tokens, meta = {}) {
  const segments = groupBySpeaker(tokens);
  const speakers = distinctSpeakers(segments);

  const dateStr = new Date(meta.created_at || Date.now()).toLocaleString("ko-KR");
  const markdown = buildTranscriptMarkdown(segments, {
    title: meta.title || "회의록 (원본 전사)",
    dateStr,
    durationSec: meta.duration_sec,
    speakerMap: meta.speaker_map,
  });

  const outDir = path.join(projectDocsDir, "meetings");
  fs.mkdirSync(outDir, { recursive: true });
  const fileName = `${stampFromIso(meta.created_at)}-${slugify(meta.title)}-transcript.md`;
  const docPath = path.join(outDir, fileName);
  fs.writeFileSync(docPath, markdown);

  return { docPath, segments, speakers };
}

/**
 * Rewrite an existing transcript doc with an updated speaker_map (e.g. after
 * labeling 화자1 → Sean). Uses meta.segments + meta.docs_dir + meta.transcript_doc.
 */
function rewriteTranscriptDoc(meta) {
  if (!meta.segments || !meta.docs_dir || !meta.transcript_doc) return null;
  const dateStr = new Date(meta.created_at || Date.now()).toLocaleString("ko-KR");
  const markdown = buildTranscriptMarkdown(meta.segments, {
    title: meta.title || "회의록 (원본 전사)",
    dateStr,
    durationSec: meta.duration_sec,
    speakerMap: meta.speaker_map,
  });
  // transcript_doc is stored as "docs/meetings/x.md"; docs_dir is the docs/ dir
  const rel = meta.transcript_doc.replace(/^docs\//, "");
  const abs = path.join(meta.docs_dir, rel);
  fs.writeFileSync(abs, markdown);
  return abs;
}

module.exports = {
  meetingsDir,
  audioPath,
  metaPath,
  readMeta,
  writeMeta,
  updateMeta,
  storeAudio,
  deleteAudio,
  listMeetings,
  writeTranscriptDoc,
  rewriteTranscriptDoc,
  speakerLabel,
};
