"use strict";
/**
 * Meeting transcript builder (Node).
 * Pure functions: Soniox async tokens → speaker-grouped segments → markdown.
 * No IO, no side effects — unit-testable.
 */

/** ms → "mm:ss" (or "h:mm:ss" past an hour) */
function fmtTime(ms) {
  const total = Math.floor((ms || 0) / 1000);
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** Human label for a raw speaker id, honoring a {id:name} map. */
function speakerLabel(speaker, speakerMap) {
  const key = String(speaker == null ? "?" : speaker);
  if (speakerMap && speakerMap[key]) return speakerMap[key];
  return `화자${key}`;
}

/**
 * Group consecutive tokens by speaker into segments.
 * tokens: [{ text, start_ms, end_ms, speaker }]
 * → [{ speaker, start_ms, end_ms, text }]
 */
function groupBySpeaker(tokens) {
  const segments = [];
  let cur = null;
  for (const t of tokens || []) {
    const sp = t.speaker == null ? "?" : String(t.speaker);
    if (!cur || cur.speaker !== sp) {
      if (cur) segments.push(cur);
      cur = { speaker: sp, start_ms: t.start_ms ?? 0, end_ms: t.end_ms ?? 0, text: t.text || "" };
    } else {
      cur.text += t.text || "";
      cur.end_ms = t.end_ms ?? cur.end_ms;
    }
  }
  if (cur) segments.push(cur);
  // trim each segment's text
  return segments
    .map((s) => ({ ...s, text: s.text.trim() }))
    .filter((s) => s.text.length > 0);
}

/** Distinct speaker ids in appearance order. */
function distinctSpeakers(segments) {
  const seen = [];
  for (const s of segments) if (!seen.includes(s.speaker)) seen.push(s.speaker);
  return seen;
}

/**
 * Build the raw-transcript markdown doc.
 * meta: { title, dateStr, durationSec, speakerMap }
 */
function buildTranscriptMarkdown(segments, meta = {}) {
  const speakers = distinctSpeakers(segments);
  const attendees = speakers.map((sp) => speakerLabel(sp, meta.speakerMap)).join(", ");
  const durMin = meta.durationSec ? Math.round(meta.durationSec / 60) : null;

  const head = [
    `# ${meta.title || "회의록 (원본 전사)"}`,
    "",
    `- 일시: ${meta.dateStr || ""}`,
    durMin != null ? `- 길이: 약 ${durMin}분` : null,
    `- 참석자: ${attendees || "(미상)"}`,
    "",
    "> 이 문서는 Soniox 비동기 화자분리로 생성된 **원본 전사**입니다.",
    "> 정리된 회의록(요약/결정사항/액션아이템)은 별도로 생성됩니다.",
    "",
    "## 전사",
    "",
  ].filter((l) => l !== null);

  const body = segments.map((s) => {
    const who = speakerLabel(s.speaker, meta.speakerMap);
    return `**${who}** (${fmtTime(s.start_ms)}): ${s.text}`;
  });

  return head.concat(body).join("\n") + "\n";
}

module.exports = {
  fmtTime,
  speakerLabel,
  groupBySpeaker,
  distinctSpeakers,
  buildTranscriptMarkdown,
};
