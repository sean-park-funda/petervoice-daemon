"use strict";
/**
 * Meeting processing orchestrator (Node).
 * Ties Soniox async transcription + local store + (optional) bot-minutes trigger.
 * Runs in the background after an upload completes.
 */

const fs = require("fs");
const path = require("path");
const { transcribeFile } = require("./soniox-async");
const store = require("./meeting-store");

/**
 * Deterministic minutes-doc path derived from the transcript path, e.g.
 * docs/meetings/x-transcript.md → docs/meetings/x-minutes.md. A fixed path is
 * what lets re-labeling UPDATE the existing minutes instead of creating a
 * duplicate document on every label save.
 */
function minutesDocFor(transcriptRel) {
  if (!transcriptRel) return null;
  return transcriptRel.replace(/-transcript\.md$/, "-minutes.md");
}

/**
 * Resolve the Soniox key for async transcription. Sean's machine has it in
 * env (.env.secrets); other users fetch the shared key from the web with their
 * api_key. Soniox temp keys are websocket-only, so async needs the real key.
 */
async function resolveSonioxKey(config) {
  if (process.env.SONIOX_API_KEY) return process.env.SONIOX_API_KEY;
  const apiUrl = (config && config.api_url) || "https://www.peter-voice.site";
  const apiKey = config && config.api_key;
  if (!apiKey) return null;
  try {
    const res = await fetch(`${apiUrl}/api/stt/async-key`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
    });
    if (!res.ok) return null;
    const d = await res.json();
    return d.key || null;
  } catch {
    return null;
  }
}

/**
 * Ask the bot to turn the raw transcript into polished minutes.
 * Best-effort: posts a user message into the meeting's project session via
 * the PeterVoice API. The daemon picks it up and the bot writes the minutes doc.
 * The minutes file path is FIXED (meta.minutes_doc) and the bot is told to
 * update it in place if it exists — re-labeling must never spawn a second doc.
 * opts.relabel: names were corrected after minutes were already requested.
 */
async function triggerMinutes(config, meeting, transcriptDocRelPath, opts = {}) {
  const apiUrl = config.api_url || "https://www.peter-voice.site";
  const apiKey = config.api_key;
  if (!apiKey || !meeting.project) return false;

  const minutesDoc = meeting.minutes_doc || minutesDocFor(transcriptDocRelPath);
  const names = Object.values(meeting.speaker_map || {}).filter(Boolean);
  const attendees = names.length ? names.join(", ") : "미확정 (화자1/2/3 표기)";

  const text = opts.upgraded
    ? (
      `[회의록 정밀 갱신] meeting_id=${meeting.id}\n` +
      `실시간(러프) 전사로 임시 작성했던 회의의 **정밀 화자분리 전사**가 완료되었습니다.\n` +
      `새 원본 전사: ${transcriptDocRelPath}\n` +
      `참석자: ${attendees}\n` +
      `회의록 파일: ${minutesDoc}\n\n` +
      `위 회의록 문서가 있으면 **새 문서를 만들지 말고** 정밀 전사 기준으로 그 문서를 ` +
      `갱신해주세요. 없으면 전사를 읽고 해당 경로에 회의록을 작성해주세요.`
    )
    : opts.relabel
    ? (
      `[회의록 이름 갱신] meeting_id=${meeting.id}\n` +
      `화자 이름이 수정되었습니다. 새 참석자: ${attendees}\n` +
      `원본 전사(이름 반영됨): ${transcriptDocRelPath}\n` +
      `회의록 파일: ${minutesDoc}\n\n` +
      `위 회의록 문서가 있으면 **새 문서를 만들지 말고** 그 문서의 화자/참석자 이름만 ` +
      `새 이름으로 갱신해주세요. 없으면 전사를 읽고 해당 경로에 회의록을 작성해주세요.`
    )
    : (
      `[회의록 정리] meeting_id=${meeting.id}\n` +
      `회의 전사가 완료됐습니다.\n` +
      `원본 전사: ${transcriptDocRelPath}\n` +
      `참석자: ${attendees}\n` +
      `회의록 파일: ${minutesDoc}\n\n` +
      `이 전사를 읽고 정리된 회의록을 **정확히 위 회의록 파일 경로에** 작성해주세요. ` +
      `이미 그 파일이 있으면 새로 만들지 말고 덮어써 갱신하세요. ` +
      `구성: 제목, 일시, 참석자, 핵심 논의, 결정사항, 액션아이템(담당/기한). ` +
      `장황하지 않게, 회의에서 실제 오간 내용 중심으로. 참석자 이름은 위 목록만 사용하고 ` +
      `전사에 없는 내용을 지어내지 마세요.`
    );

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
    if (!res.ok) console.warn(`[meeting] ${meeting.id} minutes trigger failed: HTTP ${res.status}`);
    return res.ok;
  } catch (e) {
    console.warn(`[meeting] ${meeting.id} minutes trigger failed: ${e.message || e}`);
    return false;
  }
}

/**
 * Post a bot-side notice straight into the meeting session's chat via
 * /api/bot/reply — a plain bot message, so the agent is NOT invoked (no
 * Claude turn, no cost). Fills the silent gap between meeting end and the
 * label-or-grace-period-delayed minutes.
 */
async function postChatNotice(config, project, text) {
  const apiUrl = config.api_url || "https://www.peter-voice.site";
  const apiKey = config.api_key;
  if (!apiKey || !project) return false;
  try {
    const res = await fetch(`${apiUrl}/api/bot/reply`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${apiKey}`,
        "X-Api-Key": apiKey,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ project, text, subtype: "meeting_notice" }),
    });
    if (!res.ok) console.warn(`[meeting] chat notice failed: HTTP ${res.status} (${project})`);
    return res.ok;
  } catch (e) {
    console.warn(`[meeting] chat notice failed: ${e.message || e} (${project})`);
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
  const meta = store.readMeta(configDir, meetingId);
  if (!meta) { out(`[meeting] ${meetingId} meta missing`); return; }

  const sonioxKey = await resolveSonioxKey(config);
  if (!sonioxKey) {
    // attempt counted so the sweep's precise-retry cap applies to key outages too
    store.updateMeta(configDir, meetingId, {
      status: "failed", error: "Soniox 키를 가져오지 못했습니다",
      async_attempts: (meta.async_attempts || 0) + 1,
    });
    out(`[meeting] ${meetingId} failed: no Soniox key (local + web both unavailable)`);
    return;
  }

  try {
    store.updateMeta(configDir, meetingId, { status: "processing", phase: "transcribing" });
    const audio = store.audioPath(configDir, meetingId);

    const { tokens } = await transcribeFile(sonioxKey, audio, {
      audioDurationSec: meta.duration_sec || 0,
      onProgress: (p) => {
        store.updateMeta(configDir, meetingId, { phase: p });
        out(`[meeting] ${meetingId}: ${p}`);
      },
    });

    if (!projectDocsDir) {
      store.updateMeta(configDir, meetingId, { status: "failed", error: "프로젝트 docs 경로 없음" });
      return;
    }

    // Speakers come straight from Soniox native diarization (화자1/2/3...).
    // No local voice-profile auto-recognition — the user labels every speaker
    // manually after the meeting.
    const speakerMap = {};
    const { docPath, speakers, segments } = store.writeTranscriptDoc(projectDocsDir, tokens, { ...meta, speaker_map: speakerMap });

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

    const updated = store.updateMeta(configDir, meetingId, {
      status: "transcribed",
      phase: "done",
      transcript_doc: transcriptRel,
      // A meeting retried after a fallback already has a minutes doc — keep
      // that path so the bot updates the existing minutes in place.
      minutes_doc: meta.minutes_doc || minutesDocFor(transcriptRel),
      docs_dir: projectDocsDir,
      speakers,
      speaker_map: speakerMap,
      unknown_speakers: speakers, // all speakers need a manual label
      speaker_samples: speakerSamples,
      segments,
    });
    out(`[meeting] ${meetingId} transcribed → ${transcriptRel} (${speakers.length} speakers)`);

    // A precise retry supersedes an earlier fallback doc — remove it so the
    // docs tab doesn't show two transcripts for one meeting.
    if (meta.transcript_doc && meta.transcript_doc !== transcriptRel
        && /-fallback-transcript\.md$/.test(meta.transcript_doc) && meta.docs_dir) {
      try { fs.unlinkSync(path.join(meta.docs_dir, meta.transcript_doc.replace(/^docs\//, ""))); } catch { /* best-effort */ }
    }

    // Audio is no longer needed once transcribed → delete to cap local disk use.
    try { store.deleteAudio(configDir, meetingId); } catch { /* best-effort */ }

    if (meta.minutes_requested_at) {
      // Minutes already went out based on the rough live transcript (fallback).
      // Tell the bot to refresh them from the precise transcript.
      const triggered = await triggerMinutes(config || {}, updated, transcriptRel, { upgraded: true });
      store.updateMeta(configDir, meetingId, {
        status: triggered ? "minutes_pending" : "transcribed",
        ...(triggered ? { minutes_requested_at: new Date().toISOString() } : {}),
      });
      out(`[meeting] ${meetingId} precise-upgrade minutes ${triggered ? "triggered" : "NOT triggered"}`);
    } else {
      // NOTE: minutes are NOT triggered here. The user labels speakers first, and
      // labelMeeting (or the auto-minutes checker after the grace period) triggers
      // minutes with the resolved names — otherwise the bot guesses names.
      out(`[meeting] ${meetingId} awaiting speaker labels before minutes`);
      const durMin = Math.max(1, Math.round((updated.duration_sec || meta.duration_sec || 0) / 60));
      await postChatNotice(config || {}, updated.project,
        `🎙️ 회의 전사가 저장되었습니다 (약 ${durMin}분, 화자 ${speakers.length}명).\n` +
        `- 전체 대화 기록: ${transcriptRel} (문서 탭에서 확인)\n` +
        `- 회의 화면에서 화자 이름을 입력하면 실명으로 회의록을 정리합니다.\n` +
        `- 이름 입력이 없으면 약 ${AUTO_MINUTES_AFTER_MIN}분 후 화자1/화자2 표기로 자동 정리됩니다.`);
    }
  } catch (e) {
    // Fallback: if async diarization failed but we captured a live transcript,
    // save that so the meeting isn't lost.
    const ok = await fallbackToLiveTranscript({
      configDir, config, meta, projectDocsDir,
      reason: String(e.message || e), out,
    });
    if (ok) return;
    store.updateMeta(configDir, meetingId, {
      status: "failed", error: String(e.message || e),
      async_attempts: (meta.async_attempts || 0) + 1,
    });
    out(`[meeting] ${meetingId} error: ${e.message || e}`);
  }
}

/**
 * Save the live (realtime) transcript as a fallback doc and hand it to the bot
 * for minutes. Shared by the async-failure path and the stuck-meeting sweep.
 * Returns true when the meeting was recovered this way.
 */
async function fallbackToLiveTranscript({ configDir, config, meta, projectDocsDir, reason, out }) {
  const meetingId = meta.id;
  const fallback = (meta.live_transcript || "").trim();
  if (!fallback || !projectDocsDir) return false;
  try {
    const dateStr = new Date(meta.created_at || Date.now()).toLocaleString("ko-KR");
    const md =
      `# ${meta.title || "회의록 (실시간 전사 — 폴백)"}\n\n` +
      `- 일시: ${dateStr}\n` +
      `> ⚠️ 정밀 화자분리(async)에 실패하여 **실시간 전사(러프)**로 대체했습니다.\n` +
      `> 사유: ${reason}\n\n## 전사 (실시간)\n\n${fallback}\n`;
    const outDir = path.join(projectDocsDir, "meetings");
    fs.mkdirSync(outDir, { recursive: true });
    const p = (n) => String(n).padStart(2, "0");
    const d = new Date(meta.created_at || Date.now());
    const stamp = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
    const docPath = path.join(outDir, `${stamp}-fallback-transcript.md`);
    fs.writeFileSync(docPath, md);
    const rel = docPath.split("/docs/").pop();
    const fallbackRel = rel ? `docs/${rel}` : docPath;
    const updated = store.updateMeta(configDir, meetingId, {
      status: "transcribed", phase: "fallback",
      transcript_doc: fallbackRel,
      minutes_doc: meta.minutes_doc || minutesDocFor(fallbackRel),
      docs_dir: projectDocsDir,
      error: `async 실패, 실시간 전사로 대체: ${reason}`,
      // Count the failed precise attempt; the sweep retries while audio remains
      // (capped) and this counter stops an endless retry loop.
      async_attempts: (meta.async_attempts || 0) + 1,
    });
    out(`[meeting] ${meetingId} fallback to live transcript`);
    // Even when async failed, still hand the (live) transcript to the bot so
    // the meeting gets summarized — otherwise the project agent never hears
    // about it and no minutes are produced. Skip when minutes already went out
    // (a retried fallback would spam the bot with the same content).
    const already = !!meta.minutes_requested_at;
    const triggered = already ? false : await triggerMinutes(config || {}, updated, fallbackRel);
    store.updateMeta(configDir, meetingId, {
      status: (triggered || already) ? "minutes_pending" : "transcribed",
      ...(triggered ? { minutes_requested_at: new Date().toISOString() } : {}),
    });
    out(`[meeting] ${meetingId} fallback minutes ${already ? "already requested" : triggered ? "triggered" : "NOT triggered"}`);
    return true;
  } catch (e2) {
    out(`[meeting] ${meetingId} fallback write failed: ${e2.message}`);
    return false;
  }
}

// A meeting stuck in "processing" whose meta hasn't been touched for this long
// is considered orphaned (e.g. Home Portal restarted mid-transcription — the
// AutoUpdater restarts it whenever meeting code changes). transcribeFile emits
// a heartbeat while polling, so an actively-processing meeting stays fresh.
const STUCK_AFTER_MIN = 30;
// Precise-retry policy for meetings that ended in fallback/failed while their
// audio is still on disk: retry up to N times within the retention window,
// then (and past the window) delete the residual audio. Audio of successfully
// transcribed meetings is still deleted immediately — this only governs
// leftovers from failures.
const MAX_ASYNC_ATTEMPTS = 2;
const AUDIO_RETENTION_MS = 48 * 60 * 60 * 1000;

/**
 * Recover meetings orphaned in "processing". Run at Home Portal startup and
 * periodically. Audio still on disk → retry transcription; audio gone → fall
 * back to the live transcript; neither → mark failed (nothing to recover).
 * Also: retry precise transcription for fallback/failed meetings whose audio
 * survived (network blip during polling etc.), and clean up residual audio
 * past the retention window.
 */
async function sweepStuckMeetings({ configDir, config, log }) {
  const out = log || (() => {});
  let metas = [];
  try { metas = store.listMeetings(configDir); } catch { return; }
  const cutoff = Date.now() - STUCK_AFTER_MIN * 60 * 1000;
  for (const meta of metas) {
    const id = meta.id;
    const audio = store.audioPath(configDir, id);
    const hasAudio = (() => { try { return fs.existsSync(audio); } catch { return false; } })();

    if (meta.status === "processing") {
      const touched = Date.parse(meta.updated_at || meta.created_at || "") || 0;
      if (touched > cutoff) continue; // still fresh — likely actively processing
      if (hasAudio && meta.docs_dir) {
        out(`[meeting] sweep: ${id} stuck in processing — retrying transcription`);
        await processMeeting({ configDir, config, meetingId: id, projectDocsDir: meta.docs_dir, log: out })
          .catch((e) => out(`[meeting] sweep retry failed: ${e.message}`));
      } else if ((meta.live_transcript || "").trim() && meta.docs_dir) {
        out(`[meeting] sweep: ${id} audio unavailable — falling back to live transcript`);
        await fallbackToLiveTranscript({
          configDir, config, meta, projectDocsDir: meta.docs_dir,
          reason: "처리 중단(홈포탈 재시작 추정)", out,
        });
      } else {
        out(`[meeting] sweep: ${id} unrecoverable — marking failed`);
        store.updateMeta(configDir, id, { status: "failed", error: "처리 중단(복구 불가: 오디오·전사 없음)" });
      }
      continue;
    }

    // Fallback/failed but audio survived → the precise transcript is still
    // attainable. Retry within the cap/window; on success processMeeting
    // replaces the fallback doc and refreshes the minutes in place.
    const attempts = meta.async_attempts || 0;
    const ageMs = Date.now() - (Date.parse(meta.created_at || "") || 0);
    const failedOrFallback = meta.status === "failed" || meta.phase === "fallback";
    if (hasAudio && failedOrFallback && meta.docs_dir
        && attempts < MAX_ASYNC_ATTEMPTS && ageMs < AUDIO_RETENTION_MS) {
      out(`[meeting] sweep: ${id} retrying precise transcription (attempt ${attempts + 1}/${MAX_ASYNC_ATTEMPTS})`);
      await processMeeting({ configDir, config, meetingId: id, projectDocsDir: meta.docs_dir, log: out })
        .catch((e) => out(`[meeting] sweep precise retry failed: ${e.message}`));
      continue;
    }

    // Residual audio past the retention window (retries exhausted or too old).
    if (hasAudio && ageMs > AUDIO_RETENTION_MS) {
      store.deleteAudio(configDir, id);
      out(`[meeting] sweep: ${id} residual audio removed (48h retention)`);
    }
  }
}

// Grace period for manual speaker labeling before minutes are auto-triggered.
const AUTO_MINUTES_AFTER_MIN = 10;

/**
 * Auto-trigger minutes for transcribed meetings that were never labeled.
 * The label UI triggers minutes on save — but if the user closed the tab (or
 * the meeting auto-stopped at the cap while unattended), nothing would ever
 * produce minutes. After the grace period, send the transcript to the bot
 * with 화자N names; a later label save still renames everything in place via
 * the relabel path. Run every few minutes from the Home Portal.
 */
async function autoTriggerPendingMinutes({ configDir, config, log }) {
  const out = log || (() => {});
  let metas = [];
  try { metas = store.listMeetings(configDir); } catch { return; }
  const cutoff = Date.now() - AUTO_MINUTES_AFTER_MIN * 60 * 1000;
  for (const meta of metas) {
    if (meta.status !== "transcribed" || meta.phase !== "done") continue;
    if (meta.minutes_requested_at || !meta.transcript_doc) continue;
    const t = Date.parse(meta.updated_at || meta.created_at || "") || 0;
    if (t > cutoff) continue; // still within the labeling grace period
    const triggered = await triggerMinutes(config || {}, meta, meta.transcript_doc);
    if (triggered) {
      store.updateMeta(configDir, meta.id, {
        status: "minutes_pending",
        minutes_requested_at: new Date().toISOString(),
        minutes_auto: true,
      });
      out(`[meeting] ${meta.id} auto-triggered minutes (unlabeled after ${AUTO_MINUTES_AFTER_MIN}m)`);
    }
  }
}

/**
 * Apply user labels to a meeting: set each speaker's display name, rewrite the
 * transcript doc with the resolved names, then hand the (now named) transcript
 * to the bot for minutes. Name substitution only — no voice-profile enrollment.
 * Triggering minutes HERE (not at transcription) is what lets the summary use
 * the real names instead of 화자N. Idempotent: re-labeling re-triggers with the
 * latest names; if minutes were already requested we still re-send so the bot
 * picks up corrections.
 * labels: { "<speakerId>": "<name>" } — blank clears that speaker's name.
 */
async function labelMeeting({ configDir, config, meetingId, labels }) {
  const meta = store.readMeta(configDir, meetingId);
  if (!meta) throw new Error("meeting not found");
  const speakerMap = { ...(meta.speaker_map || {}) };

  for (const [sp, raw] of Object.entries(labels || {})) {
    const next = String(raw || "").trim();
    if (next) speakerMap[sp] = next;
    else delete speakerMap[sp];
  }

  // speakers still without a name remain "unknown" (the label UI shows them)
  const unknown = (meta.speakers || []).filter((sp) => !speakerMap[sp]);
  const updated = store.updateMeta(configDir, meetingId, {
    speaker_map: speakerMap,
    unknown_speakers: unknown,
  });
  const rewritten = store.rewriteTranscriptDoc(updated);

  // Hand off to the bot for polished minutes now that names are applied.
  // If minutes were already requested for this meeting, send a rename-update
  // instead — the fixed minutes_doc path keeps it a single document either way.
  const relabel = !!meta.minutes_requested_at;
  const triggered = await triggerMinutes(config || {}, updated || meta, (updated || meta).transcript_doc, { relabel });
  store.updateMeta(configDir, meetingId, {
    status: triggered ? "minutes_pending" : "transcribed",
    ...(triggered ? { minutes_requested_at: new Date().toISOString() } : {}),
  });

  return { speaker_map: speakerMap, rewritten: !!rewritten, minutes_triggered: triggered };
}

module.exports = { processMeeting, triggerMinutes, labelMeeting, sweepStuckMeetings, autoTriggerPendingMinutes };
