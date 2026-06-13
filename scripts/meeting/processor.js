"use strict";
/**
 * Meeting processing orchestrator (Node).
 * Ties Soniox async transcription + local store + (optional) bot-minutes trigger.
 * Runs in the background after an upload completes.
 */

const { transcribeFile } = require("./soniox-async");
const store = require("./meeting-store");
const { groupBySpeaker } = require("./transcript");
const voiceProfiles = require("./voice-profiles");

// Minimum speaker audio (sec) to consider enrolling an unknown speaker
const ENROLL_MIN_SEC = 8;

/**
 * Ask the bot to turn the raw transcript into polished minutes.
 * Best-effort: posts a user message into the meeting's project session via
 * the PeterVoice API. The daemon picks it up and the bot writes the minutes doc.
 */
async function triggerMinutes(config, meeting, transcriptDocRelPath) {
  const apiUrl = config.api_url || "https://peter-voice.vercel.app";
  const apiKey = config.api_key;
  if (!apiKey || !meeting.project) return false;

  const text =
    `[회의록 정리] 방금 회의 전사가 완료됐습니다.\n` +
    `원본 전사: ${transcriptDocRelPath}\n\n` +
    `이 전사를 읽고 정리된 회의록을 docs/meetings/ 에 마크다운으로 작성해주세요. ` +
    `구성: 제목, 일시, 참석자, 핵심 논의, 결정사항, 액션아이템(담당/기한). ` +
    `장황하지 않게, 회의에서 실제 오간 내용 중심으로.`;

  try {
    const res = await fetch(`${apiUrl}/api/bot/message`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${apiKey}`,
        "X-Api-Key": apiKey,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        project: meeting.project,
        text,
        type: "user",
        subtype: "meeting_minutes_request",
        processed: false,
      }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * Process one meeting end-to-end.
 * configDir: ~/.claude-daemon ; config: parsed config.json
 * projectDocsDir: absolute docs/ dir of the meeting's project
 */
async function processMeeting({ configDir, config, meetingId, projectDocsDir, log }) {
  const out = log || (() => {});
  const sonioxKey = process.env.SONIOX_API_KEY;
  if (!sonioxKey) {
    store.updateMeta(configDir, meetingId, { status: "failed", error: "SONIOX_API_KEY 미설정" });
    out(`[meeting] ${meetingId} failed: no SONIOX_API_KEY`);
    return;
  }

  const meta = store.readMeta(configDir, meetingId);
  if (!meta) { out(`[meeting] ${meetingId} meta missing`); return; }

  try {
    store.updateMeta(configDir, meetingId, { status: "processing", phase: "transcribing" });
    const audio = store.audioPath(configDir, meetingId);

    const { tokens } = await transcribeFile(sonioxKey, audio, {
      onProgress: (p) => {
        store.updateMeta(configDir, meetingId, { phase: p });
        out(`[meeting] ${meetingId}: ${p}`);
      },
    });

    if (!projectDocsDir) {
      store.updateMeta(configDir, meetingId, { status: "failed", error: "프로젝트 docs 경로 없음" });
      return;
    }

    // ── Speaker identification via local voice profiles (best-effort) ──
    // Registered speakers → auto-named; unknown speakers with enough audio
    // are flagged so the UI can ask "who was this?" and enroll them.
    let speakerMap = {};
    let unknownSpeakers = [];
    let speakerEmbeddings = {};
    try {
      store.updateMeta(configDir, meetingId, { phase: "identifying" });
      const segs = groupBySpeaker(tokens).map((s) => ({
        speaker: s.speaker, start_ms: s.start_ms, end_ms: s.end_ms,
      }));
      const { embeddings, durations } = voiceProfiles.embedSpeakers(audio, segs);
      speakerEmbeddings = embeddings;
      const profiles = voiceProfiles.loadProfiles(configDir);
      for (const [sp, emb] of Object.entries(embeddings)) {
        const m = voiceProfiles.matchSpeaker(emb, profiles);
        if (m) speakerMap[sp] = m.name;
        else if ((durations[sp] || 0) >= ENROLL_MIN_SEC) unknownSpeakers.push(sp);
      }
      out(`[meeting] ${meetingId} speaker-id: matched=${JSON.stringify(speakerMap)} unknown=[${unknownSpeakers}]`);
    } catch (e) {
      out(`[meeting] ${meetingId} speaker-id skipped: ${e.message}`);
    }

    const metaForDoc = { ...meta, speaker_map: speakerMap };
    const { docPath, speakers, segments } = store.writeTranscriptDoc(projectDocsDir, tokens, metaForDoc);

    // Representative lines per speaker so labeling UI can show "who said what"
    const speakerSamples = {};
    for (const s of segments) {
      (speakerSamples[s.speaker] = speakerSamples[s.speaker] || []).push(s);
    }
    for (const sp of Object.keys(speakerSamples)) {
      speakerSamples[sp] = speakerSamples[sp]
        .slice().sort((a, b) => b.text.length - a.text.length).slice(0, 5)
        .sort((a, b) => a.start_ms - b.start_ms)
        .map((s) => s.text);
    }
    const rel = docPath.split("/docs/").pop();
    const transcriptRel = rel ? `docs/${rel}` : docPath;

    store.updateMeta(configDir, meetingId, {
      status: "transcribed",
      phase: "done",
      transcript_doc: transcriptRel,
      docs_dir: projectDocsDir,
      speakers,
      speaker_map: speakerMap,
      unknown_speakers: unknownSpeakers,
      speaker_embeddings: speakerEmbeddings,
      speaker_samples: speakerSamples,
      segments,
    });
    out(`[meeting] ${meetingId} transcribed → ${transcriptRel} (${speakers.length} speakers)`);

    // Phase 1B: hand off to the bot for polished minutes (best-effort)
    const triggered = await triggerMinutes(config || {}, meta, transcriptRel);
    store.updateMeta(configDir, meetingId, { status: triggered ? "minutes_pending" : "transcribed" });
  } catch (e) {
    // Fallback: if async diarization failed but we captured a live transcript,
    // save that so the meeting isn't lost.
    const fallback = (meta.live_transcript || "").trim();
    if (fallback && projectDocsDir) {
      try {
        const fs = require("fs");
        const path = require("path");
        const dateStr = new Date(meta.created_at || Date.now()).toLocaleString("ko-KR");
        const md =
          `# ${meta.title || "회의록 (실시간 전사 — 폴백)"}\n\n` +
          `- 일시: ${dateStr}\n` +
          `> ⚠️ 정밀 화자분리(async)에 실패하여 **실시간 전사(러프)**로 대체했습니다.\n` +
          `> 사유: ${String(e.message || e)}\n\n## 전사 (실시간)\n\n${fallback}\n`;
        const outDir = path.join(projectDocsDir, "meetings");
        fs.mkdirSync(outDir, { recursive: true });
        const p = (n) => String(n).padStart(2, "0");
        const d = new Date(meta.created_at || Date.now());
        const stamp = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
        const docPath = path.join(outDir, `${stamp}-fallback-transcript.md`);
        fs.writeFileSync(docPath, md);
        const rel = docPath.split("/docs/").pop();
        store.updateMeta(configDir, meetingId, {
          status: "transcribed", phase: "fallback",
          transcript_doc: rel ? `docs/${rel}` : docPath,
          error: `async 실패, 실시간 전사로 대체: ${String(e.message || e)}`,
        });
        out(`[meeting] ${meetingId} fallback to live transcript`);
        return;
      } catch (e2) {
        out(`[meeting] ${meetingId} fallback write failed: ${e2.message}`);
      }
    }
    store.updateMeta(configDir, meetingId, { status: "failed", error: String(e.message || e) });
    out(`[meeting] ${meetingId} error: ${e.message || e}`);
  }
}

/**
 * Apply user labels to a meeting: enroll each labeled speaker's embedding
 * under the given name and rewrite the transcript doc with real names.
 * labels: { "<speakerId>": "<name>" }
 */
function labelMeeting({ configDir, meetingId, labels }) {
  const meta = store.readMeta(configDir, meetingId);
  if (!meta) throw new Error("meeting not found");
  const embeddings = meta.speaker_embeddings || {};
  const speakerMap = { ...(meta.speaker_map || {}) };
  let enrolled = 0;

  for (const [sp, name] of Object.entries(labels || {})) {
    const clean = String(name || "").trim();
    if (!clean) continue;
    speakerMap[sp] = clean;
    const emb = embeddings[sp];
    if (Array.isArray(emb)) { voiceProfiles.enroll(configDir, clean, emb); enrolled++; }
  }

  // remaining unknowns = eligible speakers still without a name
  const unknown = (meta.unknown_speakers || []).filter((sp) => !speakerMap[sp]);
  const updated = store.updateMeta(configDir, meetingId, {
    speaker_map: speakerMap,
    unknown_speakers: unknown,
  });
  const rewritten = store.rewriteTranscriptDoc(updated);
  return { speaker_map: speakerMap, enrolled, rewritten: !!rewritten };
}

module.exports = { processMeeting, triggerMinutes, labelMeeting };
