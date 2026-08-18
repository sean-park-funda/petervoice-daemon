#!/usr/bin/env node
/**
 * PeterVoice Home Portal — 유저 맥미니 대시보드
 * 경량 웹서버 (순수 Node.js, 외부 의존성 없음)
 *
 * Usage: node home-portal.js [--port 3000] [--config-dir ~/.claude-daemon]
 */

const http = require("http");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const os = require("os");
const { execSync, spawnSync, spawn } = require("child_process");

// ─── Meeting mode (modular) ──────────────────────────
let meetingStore = null, processMeeting = null, meetingLabel = null, meetingSweep = null, meetingAutoMinutes = null;
try {
  const MDIR = path.join(path.dirname(__filename), "meeting");
  meetingStore = require(path.join(MDIR, "meeting-store"));
  ({ processMeeting, labelMeeting: meetingLabel, sweepStuckMeetings: meetingSweep,
    autoTriggerPendingMinutes: meetingAutoMinutes } = require(path.join(MDIR, "processor")));
} catch (e) {
  console.warn("[meeting] module not available:", e.message);
}

// ─── Terminal (WebSocket + node-pty) ─────────────────
let ptyModule = null;
let WebSocketServer = null;
let WSClient = null; // CDP 브라우저 인계용 WebSocket 클라이언트
// node-pty(네이티브, 터미널용)와 ws(순수 JS)는 따로 로드한다 —
// 한 블록에 묶으면 node-pty 빌드 실패가 ws(브라우저 인계)까지 죽인다 (뉴넥스에서 실측)
try {
  ptyModule = require(path.join(path.dirname(__filename), "node_modules/@homebridge/node-pty-prebuilt-multiarch"));
} catch (e) {
  console.warn("[terminal] node-pty not available:", e.message);
}
try {
  const wsMod = require(path.join(path.dirname(__filename), "node_modules/ws"));
  WebSocketServer = wsMod.WebSocketServer;
  WSClient = wsMod.WebSocket;
} catch (e) {
  console.warn("[terminal] ws not available:", e.message);
}

// ─── Config ──────────────────────────────────────────
const args = process.argv.slice(2);
const PORT = parseInt(getArg("--port") || "3000");
const CONFIG_DIR = getArg("--config-dir") || path.join(os.homedir(), ".claude-daemon");
const SITES_FILE = path.join(os.homedir(), ".petervoice-sites", "sites.json");
// 프로젝트 디렉토리: 두 곳 모두 탐색 (기존 ~/Projects + 신규 ~/.claude-daemon/projects/)
const PROJECTS_DIRS = [
  path.join(os.homedir(), "Projects"),
  path.join(CONFIG_DIR, "projects"),
].filter(d => fs.existsSync(d));

function getArg(name) {
  const idx = args.indexOf(name);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : null;
}

// ─── Load synced secrets (~/.claude-daemon/.env.secrets) into process.env ───
// The daemon syncs Supabase secrets to this file, but the home-portal launchd
// process only inherits PATH. Load them so features like meeting-mode STT work.
function loadSecretsEnv() {
  const secretsFile = path.join(CONFIG_DIR, ".env.secrets");
  try {
    const text = fs.readFileSync(secretsFile, "utf-8");
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const eq = trimmed.indexOf("=");
      if (eq <= 0) continue;
      const key = trimmed.slice(0, eq).trim();
      let val = trimmed.slice(eq + 1).trim();
      if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
        val = val.slice(1, -1);
      }
      if (!(key in process.env)) process.env[key] = val;
    }
  } catch { /* file may not exist yet */ }
}
loadSecretsEnv();

// ─── Ensure ffmpeg (meeting mode: container remux so Soniox gets a duration) ───
// Non-blocking: installs in the background via brew if ffmpeg is missing.
function ensureFfmpeg() {
  const paths = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"];
  if (paths.some((p) => { try { return fs.existsSync(p); } catch { return false; } })) return;
  try {
    const which = spawnSync("which", ["ffmpeg"], { encoding: "utf-8" });
    if (which.status === 0 && which.stdout.trim()) return;
  } catch { /* ignore */ }
  const brew = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"].find((b) => {
    try { return fs.existsSync(b); } catch { return false; }
  });
  if (!brew) { console.warn("[ffmpeg] missing and brew not found — meeting transcription may fail"); return; }
  console.log("[ffmpeg] not found — installing via brew in background...");
  const child = spawn(brew, ["install", "ffmpeg"], {
    env: { ...process.env, HOMEBREW_NO_AUTO_UPDATE: "1" },
    detached: true, stdio: "ignore",
  });
  child.on("exit", (code) => console.log(`[ffmpeg] brew install exited ${code}`));
  child.unref();
}
try { ensureFfmpeg(); } catch (e) { console.warn("[ffmpeg] ensure failed:", e.message); }

function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(path.join(CONFIG_DIR, "config.json"), "utf-8"));
  } catch { return {}; }
}

// ─── Stuck-meeting sweep ─────────────────────────────
// A restart of this process (AutoUpdater does it whenever meeting code changes)
// kills in-flight transcriptions, leaving meetings stuck in "processing".
// Sweep shortly after boot and periodically to retry or fall back.
// 중단된 업로드가 남긴 임시 청크(.part/.state) 청소 — 24h 지난 것만
function cleanStaleMeetingParts() {
  try {
    const dir = path.join(os.tmpdir(), "pv-meeting-chunks");
    if (!fs.existsSync(dir)) return;
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    for (const f of fs.readdirSync(dir)) {
      const p = path.join(dir, f);
      try { if (fs.statSync(p).mtimeMs < cutoff) fs.unlinkSync(p); } catch { /* ignore */ }
    }
  } catch { /* ignore */ }
}

// 스윕/자동회의록 공통 실행기: 셀프호스트는 CONFIG_DIR 1건, 클라우드는 유저별
// (<usersRoot>/<uid>) 반복 — 회의록 트리거가 그 유저의 api_key 로 나가야 한다.
async function forEachMeetingBase(fn) {
  for (const t of meetingSweepTargets()) {
    const config = t.uid
      ? { api_url: cloudApiUrl(), api_key: await cloudUserApiKey(t.uid) }
      : loadConfig();
    await Promise.resolve(fn(t.configDir, config))
      .catch((e) => console.warn(`[meeting] base ${t.uid || "local"} failed:`, e.message));
  }
}

if (meetingSweep) {
  const runSweep = () => {
    cleanStaleMeetingParts();
    forEachMeetingBase((configDir, config) => meetingSweep({ configDir, config, log: (m) => console.log(m) }))
      .catch((e) => console.warn("[meeting] sweep failed:", e.message));
  };
  setTimeout(runSweep, 60 * 1000);
  setInterval(runSweep, 6 * 60 * 60 * 1000);
}

// 라벨 미입력 회의 자동 회의록: 전사 후 10분 내 이름 입력이 없으면 화자N으로라도
// 회의록을 트리거한다 — 탭을 닫아버린 회의가 회의록 없이 방치되는 구멍 방지.
if (meetingAutoMinutes) {
  const runAutoMinutes = () => {
    forEachMeetingBase((configDir, config) => meetingAutoMinutes({ configDir, config, log: (m) => console.log(m) }))
      .catch((e) => console.warn("[meeting] auto-minutes failed:", e.message));
  };
  setTimeout(runAutoMinutes, 2 * 60 * 1000);
  setInterval(runAutoMinutes, 5 * 60 * 1000);
}

function loadSites() {
  try {
    return JSON.parse(fs.readFileSync(SITES_FILE, "utf-8"));
  } catch { return {}; }
}

// ─── API Handlers ────────────────────────────────────

function apiSites() {
  const sites = loadSites();
  const result = Object.entries(sites).map(([id, s]) => {
    let running = false;
    try {
      const net = require("net");
      const sock = new net.Socket();
      // sync check not ideal, but simple
      running = s.status === "running";
    } catch {}
    return { id, ...s, running };
  });
  return result;
}

function apiProjects() {
  const sites = loadSites();
  const publishedDirs = new Set(Object.values(sites).map(s => s.project_dir));

  try {
    const results = [];
    const seen = new Set();
    for (const projDir of PROJECTS_DIRS) {
      if (!fs.existsSync(projDir)) continue;
      const entries = fs.readdirSync(projDir, { withFileTypes: true });
      for (const e of entries) {
        if (!e.isDirectory() || e.name.startsWith(".")) continue;
        if (seen.has(e.name)) continue;
        seen.add(e.name);

        const dir = path.join(projDir, e.name);
        let framework = "unknown";
        const pkgPath = path.join(dir, "package.json");
        const indexPath = path.join(dir, "index.html");

        if (fs.existsSync(pkgPath)) {
          try {
            const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf-8"));
            const deps = { ...pkg.dependencies, ...pkg.devDependencies };
            if (deps && deps.next) framework = "nextjs";
            else if (deps && deps.vite) framework = "vite";
            else framework = "node";
          } catch { framework = "node"; }
        } else if (fs.existsSync(indexPath)) {
          framework = "static";
        }

        const published = publishedDirs.has(dir);
        const siteEntry = published
          ? Object.entries(sites).find(([, s]) => s.project_dir === dir)
          : null;

        results.push({
          name: e.name,
          dir,
          framework,
          published,
          url: siteEntry ? siteEntry[1].url : null,
          port: siteEntry ? siteEntry[1].port : null,
        });
      }
    }
    return results;
  } catch { return []; }
}

function apiSystem() {
  const uptime = os.uptime();
  const days = Math.floor(uptime / 86400);
  const hours = Math.floor((uptime % 86400) / 3600);
  const totalMem = (os.totalmem() / 1073741824).toFixed(1);
  const freeMem = (os.freemem() / 1073741824).toFixed(1);
  const usedMem = (totalMem - freeMem).toFixed(1);

  let diskPercent = "?";
  try {
    const df = execSync("df -h / | tail -1", { encoding: "utf-8" });
    const parts = df.trim().split(/\s+/);
    diskPercent = parts[4] || "?";
  } catch {}

  let cloudflared = false;
  try {
    execSync("pgrep -f 'cloudflared.*tunnel.*run'", { encoding: "utf-8" });
    cloudflared = true;
  } catch {}

  let daemon = false;
  try {
    execSync("pgrep -f 'claude_daemon'", { encoding: "utf-8" });
    daemon = true;
  } catch {}

  let nodeVersion = "?";
  try {
    nodeVersion = execSync("node --version", { encoding: "utf-8" }).trim();
  } catch {}

  return {
    uptime: `${days}d ${hours}h`,
    disk: diskPercent,
    memory: `${usedMem}/${totalMem}GB`,
    cloudflared,
    daemon,
    nodeVersion,
    hostname: os.hostname(),
  };
}

// ─── Git API Handlers ────────────────────────────────

function validateGitDir(dir) {
  // 보안: 허용된 프로젝트 디렉토리 하위만 접근 가능
  const resolved = path.resolve(dir);
  for (const projDir of PROJECTS_DIRS) {
    if (resolved.startsWith(path.resolve(projDir))) return resolved;
  }
  // 홈 디렉토리 하위 허용 (데몬 프로젝트 등)
  if (resolved.startsWith(os.homedir())) return resolved;
  return null;
}

function apiGitRepos(dir) {
  const validated = validateGitDir(dir);
  if (!validated) return { error: "접근 불가 경로" };
  if (!fs.existsSync(validated)) return { error: "경로 없음" };

  // dir 자체가 git repo인지 확인
  if (fs.existsSync(path.join(validated, ".git"))) {
    return { repos: [gitRepoInfo(validated)] };
  }

  // 하위 디렉토리에서 git repo 탐색 (1레벨만)
  const repos = [];
  try {
    const entries = fs.readdirSync(validated, { withFileTypes: true });
    for (const e of entries) {
      if (!e.isDirectory() || e.name.startsWith(".")) continue;
      const sub = path.join(validated, e.name);
      if (fs.existsSync(path.join(sub, ".git"))) {
        repos.push(gitRepoInfo(sub));
      }
    }
  } catch {}
  return { repos };
}

function gitRepoInfo(dir) {
  let remoteUrl = "";
  let defaultBranch = "main";
  try {
    remoteUrl = execSync("git -C " + JSON.stringify(dir) + " remote get-url origin 2>/dev/null", { encoding: "utf-8" }).trim();
  } catch {}
  try {
    // HEAD가 가리키는 브랜치
    defaultBranch = execSync("git -C " + JSON.stringify(dir) + " rev-parse --abbrev-ref HEAD", { encoding: "utf-8" }).trim();
  } catch {}
  return {
    repo_name: path.basename(dir),
    local_path: dir,
    remote_url: remoteUrl || null,
    default_branch: defaultBranch,
  };
}

function apiGitBranches(dir) {
  const validated = validateGitDir(dir);
  if (!validated) return { error: "접근 불가 경로" };
  try {
    const raw = execSync("git -C " + JSON.stringify(validated) + " branch --format='%(refname:short)' 2>/dev/null", { encoding: "utf-8" });
    const branches = raw.trim().split("\n").filter(Boolean);
    let current = "";
    try {
      current = execSync("git -C " + JSON.stringify(validated) + " rev-parse --abbrev-ref HEAD", { encoding: "utf-8" }).trim();
    } catch {}
    return { branches, current };
  } catch (e) {
    return { error: "git 실행 실패: " + e.message };
  }
}

function apiGitCommits(dir, branch, limit) {
  const validated = validateGitDir(dir);
  if (!validated) return { error: "접근 불가 경로" };
  try {
    const format = '{"hash":"%H","short_hash":"%h","message":"%s","author":"%an","date":"%aI"}';
    const raw = execSync(
      `git -C ${JSON.stringify(validated)} log ${JSON.stringify(branch)} --max-count=${limit} --format='${format}' --no-merges 2>/dev/null`,
      { encoding: "utf-8", maxBuffer: 10 * 1024 * 1024 }
    );
    const commits = raw.trim().split("\n").filter(Boolean).map(line => {
      try { return JSON.parse(line); } catch { return null; }
    }).filter(Boolean);

    // 각 커밋에 stat 추가
    for (const c of commits) {
      try {
        const stat = execSync(
          `git -C ${JSON.stringify(validated)} diff-tree --no-commit-id --shortstat ${c.hash} 2>/dev/null`,
          { encoding: "utf-8" }
        ).trim();
        const fm = stat.match(/(\d+) file/);
        const am = stat.match(/(\d+) insertion/);
        const dm = stat.match(/(\d+) deletion/);
        c.files_changed = fm ? parseInt(fm[1]) : 0;
        c.additions = am ? parseInt(am[1]) : 0;
        c.deletions = dm ? parseInt(dm[1]) : 0;
      } catch {
        c.files_changed = 0;
        c.additions = 0;
        c.deletions = 0;
      }
    }
    return { commits };
  } catch (e) {
    return { error: "git 실행 실패: " + e.message };
  }
}

function apiGitDiff(dir, commit) {
  const validated = validateGitDir(dir);
  if (!validated) return { error: "접근 불가 경로" };
  try {
    // 커밋의 변경 파일 목록
    const nameStatus = execSync(
      `git -C ${JSON.stringify(validated)} diff-tree --no-commit-id -r --name-status ${JSON.stringify(commit)} 2>/dev/null`,
      { encoding: "utf-8" }
    ).trim();

    const files = nameStatus.split("\n").filter(Boolean).map(line => {
      const parts = line.split("\t");
      const statusMap = { A: "added", M: "modified", D: "deleted", R: "renamed" };
      return {
        status: statusMap[parts[0]?.[0]] || "modified",
        path: parts[parts.length - 1],
      };
    });

    // 각 파일의 diff
    const fileDiffs = [];
    for (const f of files) {
      try {
        const diff = execSync(
          `git -C ${JSON.stringify(validated)} show ${JSON.stringify(commit)} -- ${JSON.stringify(f.path)} 2>/dev/null`,
          { encoding: "utf-8", maxBuffer: 5 * 1024 * 1024 }
        );
        // diff 부분만 추출 (커밋 메타 제거)
        const diffStart = diff.indexOf("diff --git");
        const diffText = diffStart >= 0 ? diff.slice(diffStart) : diff;
        const am = diffText.match(/^\+[^+]/gm);
        const dm = diffText.match(/^-[^-]/gm);
        fileDiffs.push({
          path: f.path,
          status: f.status,
          diff_text: diffText,
          additions: am ? am.length : 0,
          deletions: dm ? dm.length : 0,
        });
      } catch {
        fileDiffs.push({ path: f.path, status: f.status, diff_text: "", additions: 0, deletions: 0 });
      }
    }
    return { file_diffs: fileDiffs };
  } catch (e) {
    return { error: "git 실행 실패: " + e.message };
  }
}

function apiGitDiffRange(dir, fromCommit, toCommit) {
  const validated = validateGitDir(dir);
  if (!validated) return { error: "접근 불가 경로" };
  try {
    const raw = execSync(
      `git -C ${JSON.stringify(validated)} diff ${JSON.stringify(fromCommit)}..${JSON.stringify(toCommit)} 2>/dev/null`,
      { encoding: "utf-8", maxBuffer: 20 * 1024 * 1024 }
    );

    // 파일별로 분리
    const fileDiffs = [];
    const parts = raw.split(/^(?=diff --git)/m);
    for (const part of parts) {
      if (!part.trim()) continue;
      const pathMatch = part.match(/^diff --git a\/(.*?) b\/(.*)/m);
      if (!pathMatch) continue;
      const filePath = pathMatch[2];
      let status = "modified";
      if (part.includes("new file mode")) status = "added";
      else if (part.includes("deleted file mode")) status = "deleted";
      else if (part.includes("rename from")) status = "renamed";
      const am = part.match(/^\+[^+]/gm);
      const dm = part.match(/^-[^-]/gm);
      fileDiffs.push({
        path: filePath,
        status,
        diff_text: part,
        additions: am ? am.length : 0,
        deletions: dm ? dm.length : 0,
      });
    }
    return { file_diffs: fileDiffs };
  } catch (e) {
    return { error: "git 실행 실패: " + e.message };
  }
}

// ─── File browsing ───────────────────────────────────

function apiBrowse(relDir) {
  // 보안: 허용된 프로젝트 디렉토리 하위만 접근 가능
  let baseDir = null;
  let targetDir = null;

  for (const projDir of PROJECTS_DIRS) {
    const base = path.resolve(projDir);
    const target = path.resolve(base, relDir || "");
    if (target.startsWith(base) && fs.existsSync(target) && fs.statSync(target).isDirectory()) {
      baseDir = base;
      targetDir = target;
      break;
    }
  }

  if (!baseDir || !targetDir) {
    return { error: relDir ? "디렉토리 없음" : "접근 불가 경로" };
  }

  try {
    const entries = fs.readdirSync(targetDir, { withFileTypes: true });
    const items = entries
      .filter(e => !e.name.startsWith("."))
      .map(e => {
        const fullPath = path.join(targetDir, e.name);
        const isDir = e.isDirectory();
        const relPath = path.relative(baseDir, fullPath);
        let size = null;
        if (!isDir) {
          try { size = fs.statSync(fullPath).size; } catch {}
        }
        return {
          name: e.name,
          type: isDir ? "dir" : "file",
          path: relPath,
          size,
        };
      })
      .sort((a, b) => {
        // 폴더 먼저, 그 다음 이름순
        if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
        return a.name.localeCompare(b.name);
      });

    return {
      dir: path.relative(baseDir, targetDir) || "",
      items,
    };
  } catch {
    return { error: "읽기 실패" };
  }
}

function apiLogs(projectId) {
  const logsDir = path.join(os.homedir(), ".petervoice-sites", projectId, "logs");
  const result = {};
  for (const name of ["stdout.log", "stderr.log"]) {
    const p = path.join(logsDir, name);
    try {
      const content = fs.readFileSync(p, "utf-8");
      const lines = content.split("\n");
      result[name] = lines.slice(-50).join("\n");
    } catch {
      result[name] = "";
    }
  }
  return result;
}

// ─── HTML ────────────────────────────────────────────

function renderHTML(req) {
  const config = loadConfig();
  // 타이틀용 이름: Host 헤더에서 추출 (sean.peter-voice.site → sean), 없으면 OS 유저명
  const host = req && req.headers && req.headers.host;
  const username = (host && host.includes("."))
    ? host.split(".")[0]
    : os.userInfo().username;

  return `<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PeterVoice Home</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #0f0f17;
      color: #e0e0e0;
      min-height: 100vh;
    }
    .container { max-width: 900px; margin: 0 auto; padding: 24px 16px; }
    h1 {
      font-size: 24px;
      font-weight: 600;
      margin-bottom: 8px;
      color: #fff;
    }
    .subtitle { color: #888; font-size: 14px; margin-bottom: 32px; }
    .section {
      background: #1a1a2e;
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 20px;
    }
    .section-title {
      font-size: 14px;
      font-weight: 600;
      color: #888;
      text-transform: uppercase;
      letter-spacing: 1px;
      margin-bottom: 16px;
    }
    .site-row {
      display: flex;
      align-items: center;
      padding: 12px 0;
      border-bottom: 1px solid #2a2a3e;
    }
    .site-row:last-child { border-bottom: none; }
    .site-name { flex: 1; font-weight: 500; }
    .site-status {
      width: 8px; height: 8px;
      border-radius: 50%;
      margin-right: 12px;
    }
    .status-running { background: #4ade80; box-shadow: 0 0 6px #4ade8066; }
    .status-stopped { background: #666; }
    .site-port { color: #888; font-size: 13px; margin-right: 16px; font-family: monospace; }
    .site-link {
      color: #60a5fa;
      text-decoration: none;
      font-size: 13px;
    }
    .site-link:hover { text-decoration: underline; }
    .btn {
      padding: 6px 14px;
      border-radius: 6px;
      border: none;
      cursor: pointer;
      font-size: 12px;
      font-weight: 500;
      margin-left: 8px;
    }
    .btn-rebuild { background: #2563eb; color: #fff; }
    .btn-rebuild:hover { background: #3b82f6; }
    .btn-stop { background: #dc2626; color: #fff; }
    .btn-stop:hover { background: #ef4444; }
    .btn-publish { background: #16a34a; color: #fff; }
    .btn-publish:hover { background: #22c55e; }
    .project-row {
      display: flex;
      align-items: center;
      padding: 10px 0;
      border-bottom: 1px solid #2a2a3e;
    }
    .project-row:last-child { border-bottom: none; }
    .project-name { flex: 1; }
    .project-fw {
      background: #2a2a3e;
      color: #aaa;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11px;
      margin-right: 12px;
    }
    .fw-nextjs { color: #fff; background: #000; }
    .fw-vite { color: #bd34fe; background: #1a1a2e; border: 1px solid #bd34fe44; }
    .fw-static { color: #f59e0b; background: #1a1a2e; border: 1px solid #f59e0b44; }
    .sys-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
    }
    .sys-item {
      background: #12121e;
      border-radius: 8px;
      padding: 14px;
      text-align: center;
    }
    .sys-value { font-size: 20px; font-weight: 600; color: #fff; }
    .sys-label { font-size: 11px; color: #888; margin-top: 4px; }
    .indicator {
      display: inline-block;
      width: 8px; height: 8px;
      border-radius: 50%;
      margin-right: 6px;
    }
    .ind-on { background: #4ade80; }
    .ind-off { background: #ef4444; }
    .empty { color: #666; font-size: 14px; padding: 16px 0; text-align: center; }
    .logs-area {
      background: #12121e;
      border-radius: 8px;
      padding: 12px;
      font-family: monospace;
      font-size: 12px;
      max-height: 200px;
      overflow-y: auto;
      white-space: pre-wrap;
      color: #aaa;
      margin-top: 8px;
      display: none;
    }
    @media (max-width: 600px) {
      .site-port { display: none; }
      .sys-grid { grid-template-columns: repeat(2, 1fr); }
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>${username}'s Mac Mini</h1>
    <div class="subtitle">PeterVoice Home Portal</div>

    <div class="section">
      <div class="section-title">Published Sites</div>
      <div id="sites-list"><div class="empty">Loading...</div></div>
    </div>

    <div class="section">
      <div class="section-title">Projects</div>
      <div id="breadcrumb" style="margin-bottom:12px;font-size:13px;"></div>
      <div id="projects-list"><div class="empty">Loading...</div></div>
    </div>

    <div class="section">
      <div class="section-title">System</div>
      <div id="system-info" class="sys-grid"><div class="empty">Loading...</div></div>
    </div>
  </div>

  <script>
    async function load() {
      // Sites
      try {
        const sites = await (await fetch("/api/sites")).json();
        const el = document.getElementById("sites-list");
        if (!sites.length) {
          el.innerHTML = '<div class="empty">No published sites</div>';
        } else {
          el.innerHTML = sites.map(s => \`
            <div class="site-row">
              <div class="site-status \${s.status === 'running' ? 'status-running' : 'status-stopped'}"></div>
              <div class="site-name">\${s.id}</div>
              <div class="site-port">:\${s.port}</div>
              \${s.url ? \`<a class="site-link" href="\${s.url}" target="_blank">\${s.hostname || s.url}</a>\` : ''}
              <button class="btn btn-rebuild" onclick="rebuild('\${s.id}')">Rebuild</button>
              <button class="btn btn-stop" onclick="unpublish('\${s.id}')">Stop</button>
            </div>
          \`).join("");
        }
      } catch {}

      // Projects — load root
      await browse("");

      // System
      try {
        const sys = await (await fetch("/api/system")).json();
        document.getElementById("system-info").innerHTML = \`
          <div class="sys-item"><div class="sys-value">\${sys.uptime}</div><div class="sys-label">Uptime</div></div>
          <div class="sys-item"><div class="sys-value">\${sys.disk}</div><div class="sys-label">Disk</div></div>
          <div class="sys-item"><div class="sys-value">\${sys.memory}</div><div class="sys-label">Memory</div></div>
          <div class="sys-item"><div class="sys-value"><span class="indicator \${sys.cloudflared ? 'ind-on' : 'ind-off'}"></span>\${sys.cloudflared ? 'ON' : 'OFF'}</div><div class="sys-label">cloudflared</div></div>
          <div class="sys-item"><div class="sys-value"><span class="indicator \${sys.daemon ? 'ind-on' : 'ind-off'}"></span>\${sys.daemon ? 'ON' : 'OFF'}</div><div class="sys-label">Daemon</div></div>
          <div class="sys-item"><div class="sys-value">\${sys.nodeVersion}</div><div class="sys-label">Node.js</div></div>
        \`;
      } catch {}
    }

    let currentBrowseDir = "";

    async function browse(dir) {
      currentBrowseDir = dir;
      try {
        const data = await (await fetch("/api/browse?dir=" + encodeURIComponent(dir))).json();
        if (data.error) { document.getElementById("projects-list").innerHTML = '<div class="empty">' + data.error + '</div>'; return; }

        // Breadcrumb
        const bc = document.getElementById("breadcrumb");
        const parts = data.dir ? data.dir.split("/") : [];
        let bcHTML = '<a href="#" onclick="browse(\\'\\');return false" style="color:#60a5fa;text-decoration:none">~/Projects</a>';
        let accum = "";
        parts.forEach(p => {
          accum = accum ? accum + "/" + p : p;
          const escaped = accum.replace(/'/g, "\\\\'");
          bcHTML += ' / <a href="#" onclick="browse(\\'' + escaped + '\\');return false" style="color:#60a5fa;text-decoration:none">' + p + '</a>';
        });
        bc.innerHTML = bcHTML;

        // List
        const el = document.getElementById("projects-list");
        if (!data.items.length) {
          el.innerHTML = '<div class="empty">빈 폴더</div>';
          return;
        }

        // 퍼블리시 정보 (루트만)
        let publishedMap = {};
        if (!dir) {
          try {
            const projects = await (await fetch("/api/projects")).json();
            projects.forEach(p => { publishedMap[p.name] = p; });
          } catch {}
        }

        el.innerHTML = data.items.map(item => {
          if (item.type === "dir") {
            const escaped = item.path.replace(/'/g, "\\\\'");
            const proj = publishedMap[item.name];
            const fwBadge = proj && proj.framework !== 'unknown'
              ? '<span class="project-fw fw-' + proj.framework + '">' + proj.framework + '</span>'
              : '';
            const pubInfo = proj
              ? (proj.published
                ? '<a class="site-link" href="' + proj.url + '" target="_blank">Published</a>'
                : proj.framework !== 'unknown'
                  ? '<button class="btn btn-publish" onclick="event.stopPropagation();publish(\\'' + item.name.replace(/'/g, "\\\\'") + '\\', \\'' + proj.dir.replace(/'/g, "\\\\'") + '\\')">Publish</button>'
                  : '')
              : '';
            return '<div class="project-row" style="cursor:pointer" onclick="browse(\\'' + escaped + '\\')">' +
              '<div class="project-name" style="display:flex;align-items:center;gap:6px"><span style="opacity:0.5">📁</span> ' + item.name + '</div>' +
              fwBadge + pubInfo +
              '</div>';
          } else {
            const sizeStr = item.size != null ? formatSize(item.size) : '';
            return '<div class="project-row">' +
              '<div class="project-name" style="display:flex;align-items:center;gap:6px"><span style="opacity:0.3">📄</span> ' + item.name + '</div>' +
              '<span style="color:#666;font-size:12px;font-family:monospace">' + sizeStr + '</span>' +
              '</div>';
          }
        }).join("");
      } catch { document.getElementById("projects-list").innerHTML = '<div class="empty">로딩 실패</div>'; }
    }

    function formatSize(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
      if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
      return (bytes / 1073741824).toFixed(1) + ' GB';
    }

    async function rebuild(id) {
      if (!confirm(\`\${id} 재빌드할까요?\`)) return;
      const r = await fetch("/api/rebuild", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({project_id: id}) });
      const res = await r.json();
      alert(res.ok ? "재빌드 완료! 새로고침하세요." : "오류: " + (res.error || "unknown"));
      load();
    }

    async function unpublish(id) {
      if (!confirm(\`\${id} 사이트를 중지할까요?\`)) return;
      const r = await fetch("/api/unpublish", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({project_id: id}) });
      const res = await r.json();
      alert(res.ok ? "중지 완료" : "오류: " + (res.error || "unknown"));
      load();
    }

    async function publish(name, dir) {
      if (!confirm(\`\${name} 퍼블리시할까요?\`)) return;
      const r = await fetch("/api/publish", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({project_id: name, project_dir: dir}) });
      const res = await r.json();
      alert(res.url ? "퍼블리시 완료: " + res.url : "오류: " + (res.error || "unknown"));
      load();
    }

    load();
    setInterval(load, 30000);
  </script>
</body>
</html>`;
}

// ─── Action handlers ─────────────────────────────────

function execPublish(body) {
  const { project_id, project_dir } = body;
  if (!project_id || !project_dir) return { error: "project_id, project_dir 필수" };
  const config = loadConfig();
  const username = config.tunnel_url
    ? new URL(config.tunnel_url).hostname.split(".")[0]
    : (config.username || "user").toLowerCase().replace(/\s/g, "-");
  try {
    const script = path.join(__dirname, "publish.py");
    const out = execSync(
      `python3 "${script}" publish "${project_id}" "${project_dir}" --username "${username}"`,
      { encoding: "utf-8", timeout: 600000 }
    );
    return JSON.parse(out.trim());
  } catch (e) {
    return { error: e.message.slice(0, 500) };
  }
}

function execRebuild(body) {
  const { project_id } = body;
  if (!project_id) return { error: "project_id 필수" };
  try {
    const script = path.join(__dirname, "publish.py");
    const out = execSync(
      `python3 "${script}" rebuild "${project_id}"`,
      { encoding: "utf-8", timeout: 600000 }
    );
    return JSON.parse(out.trim());
  } catch (e) {
    return { error: e.message.slice(0, 500) };
  }
}

function execUnpublish(body) {
  const { project_id } = body;
  if (!project_id) return { error: "project_id 필수" };
  const config = loadConfig();
  const username = config.tunnel_url
    ? new URL(config.tunnel_url).hostname.split(".")[0]
    : (config.username || "user").toLowerCase().replace(/\s/g, "-");
  try {
    const script = path.join(__dirname, "publish.py");
    const out = execSync(
      `python3 "${script}" unpublish "${project_id}" --username "${username}"`,
      { encoding: "utf-8", timeout: 60000 }
    );
    return JSON.parse(out.trim());
  } catch (e) {
    return { error: e.message.slice(0, 500) };
  }
}

// ─── Docs API ───────────────────────────────────────

function validateDocsDir(dir) {
  const resolved = path.resolve(dir);
  if (CLOUD_MODE) {
    // 클라우드: 유저 워크스페이스 루트 하위만 허용.
    // 유저별 바인딩(본인 uid 폴더 강제)은 웹 프록시가 세션 기준으로 수행 — 여긴 2차 방어.
    return resolved.startsWith(path.resolve(CLOUD_USERS_ROOT) + path.sep) ? resolved : null;
  }
  // path traversal 방지: 홈 디렉토리 하위만 허용
  const homeDir = os.homedir();
  if (!resolved.startsWith(homeDir)) return null;
  return resolved;
}

function apiDocsAll() {
  const projectsMap = {};
  const daemonProjects = path.join(CONFIG_DIR, "projects");
  if (fs.existsSync(daemonProjects)) {
    for (const e of fs.readdirSync(daemonProjects, { withFileTypes: true })) {
      if (!e.isDirectory() || e.name.startsWith(".")) continue;
      projectsMap[e.name] = { name: e.name, dir: path.join(daemonProjects, e.name) };
    }
  }
  const homeProjects = path.join(os.homedir(), "Projects");
  if (fs.existsSync(homeProjects)) {
    for (const e of fs.readdirSync(homeProjects, { withFileTypes: true })) {
      if (!e.isDirectory() || e.name.startsWith(".")) continue;
      const docsDir = path.join(homeProjects, e.name, "docs");
      if (fs.existsSync(docsDir) && !projectsMap[e.name]) {
        projectsMap[e.name] = { name: e.name, dir: path.join(homeProjects, e.name) };
      }
    }
  }
  const result = [];
  for (const [id, info] of Object.entries(projectsMap)) {
    const docsDir = path.join(info.dir, "docs");
    if (!fs.existsSync(docsDir)) continue;
    const tree = apiDocsList(docsDir);
    if (!tree.documents || tree.documents.length === 0) continue;
    result.push({ id, name: id, docsDir, documents: tree.documents });
  }
  result.sort((a, b) => a.name.localeCompare(b.name));
  return { projects: result };
}

function apiDocsList(docsDir) {
  const validated = validateDocsDir(docsDir);
  if (!validated) return { error: "접근 불가 경로" };
  if (!fs.existsSync(validated)) {
    try { fs.mkdirSync(validated, { recursive: true }); } catch {}
    return { documents: [] };
  }

  // 계층 구조로 반환 (DocumentsPanel Doc 인터페이스 호환)
  const foldersMap = {}; // relPath → folder doc
  const rootDocs = [];

  function scan(dir, prefix, parentId) {
    try {
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      const sorted = entries
        .filter(e => !e.name.startsWith("."))
        .sort((a, b) => {
          if (a.isDirectory() !== b.isDirectory()) return a.isDirectory() ? -1 : 1;
          return a.name.localeCompare(b.name);
        });

      let sortOrder = 0;
      for (const entry of sorted) {
        const fullPath = path.join(dir, entry.name);
        const relPath = prefix ? `${prefix}/${entry.name}` : entry.name;

        if (entry.isDirectory()) {
          const folder = {
            id: `folder:${relPath}`,
            title: entry.name,
            content: "",
            type: "folder",
            parent_id: parentId,
            file_path: null,
            pinned: false,
            sort_order: sortOrder++,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            children: [],
          };
          foldersMap[relPath] = folder;
          if (parentId && foldersMap[prefix]) {
            foldersMap[prefix].children.push(folder);
          } else {
            rootDocs.push(folder);
          }
          scan(fullPath, relPath, `folder:${relPath}`);
        } else {
          const stat = fs.statSync(fullPath);
          const ext = path.extname(entry.name).toLowerCase();
          const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"]);
          const CODE_EXTS = new Set([".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".xml", ".sh", ".bash", ".sql", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".rb", ".php", ".swift", ".kt", ".r", ".lua", ".pl", ".ex", ".exs"]);
          const TEXT_EXTS = new Set([".txt", ".csv", ".json", ".yaml", ".yml", ".toml", ".env", ".ini", ".cfg"]);
          const VIDEO_EXTS = new Set([".mp4", ".webm", ".mov", ".avi"]);
          const AUDIO_EXTS = new Set([".mp3", ".wav", ".ogg", ".m4a", ".flac"]);
          const PDF_EXT = ".pdf";
          let fileType = "file";
          if (ext === ".md") fileType = "doc";
          else if (IMAGE_EXTS.has(ext)) fileType = "image";
          else if (CODE_EXTS.has(ext)) fileType = "code";
          else if (TEXT_EXTS.has(ext)) fileType = "text";
          else if (VIDEO_EXTS.has(ext)) fileType = "video";
          else if (AUDIO_EXTS.has(ext)) fileType = "audio";
          else if (ext === PDF_EXT) fileType = "pdf";

          const doc = {
            id: `${fileType}:${relPath}`,
            title: ext === ".md" ? entry.name.replace(/\.md$/, "") : entry.name,
            content: "",
            type: fileType,
            parent_id: parentId,
            file_path: relPath,
            pinned: false,
            sort_order: sortOrder++,
            size: stat.size,
            created_at: stat.birthtime.toISOString(),
            updated_at: stat.mtime.toISOString(),
          };
          if (parentId && foldersMap[prefix]) {
            foldersMap[prefix].children.push(doc);
          } else {
            rootDocs.push(doc);
          }
        }
      }
    } catch {}
  }
  scan(validated, "", null);
  return { documents: rootDocs };
}

function apiDocsRead(docsDir, docPath) {
  const validated = validateDocsDir(docsDir);
  if (!validated) return { error: "접근 불가 경로" };

  const filePath = path.resolve(validated, docPath);

  // path traversal 방지
  if (!filePath.startsWith(validated)) return { error: "접근 불가 경로" };
  if (!fs.existsSync(filePath)) return { error: "파일 없음" };

  // 텍스트로 읽을 수 있는 확장자만 허용
  const TEXT_EXTS = new Set([
    ".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".xml",
    ".sh", ".bash", ".zsh", ".sql", ".env", ".ini", ".cfg",
    ".rs", ".go", ".java", ".c", ".cpp", ".h", ".rb", ".php",
    ".swift", ".kt", ".r", ".lua", ".pl", ".ex", ".exs",
  ]);
  const ext = path.extname(filePath).toLowerCase();
  if (!TEXT_EXTS.has(ext)) return { error: "텍스트 파일만 지원" };

  try {
    const content = fs.readFileSync(filePath, "utf-8");
    const stat = fs.statSync(filePath);
    return {
      path: docPath,
      title: path.basename(docPath, ext),
      content,
      size: stat.size,
      modified: stat.mtime.toISOString(),
    };
  } catch {
    return { error: "읽기 실패" };
  }
}

const MIME_TYPES = {
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
  ".bmp": "image/bmp", ".pdf": "application/pdf", ".mp4": "video/mp4",
  ".webm": "video/webm", ".mp3": "audio/mpeg", ".wav": "audio/wav",
  ".json": "application/json", ".txt": "text/plain", ".csv": "text/csv",
  ".py": "text/plain", ".js": "text/plain", ".ts": "text/plain",
  ".html": "text/html", ".htm": "text/html",
};

function serveDocsFile(res, docsDir, filePath) {
  const validated = validateDocsDir(docsDir);
  if (!validated) { res.writeHead(403); res.end("Forbidden"); return; }

  const fullPath = path.resolve(validated, filePath);
  if (!fullPath.startsWith(validated)) { res.writeHead(403); res.end("Forbidden"); return; }
  if (!fs.existsSync(fullPath)) { res.writeHead(404); res.end("Not found"); return; }

  try {
    const ext = path.extname(fullPath).toLowerCase();
    const mime = MIME_TYPES[ext] || "application/octet-stream";
    const stat = fs.statSync(fullPath);
    res.writeHead(200, {
      "Content-Type": mime,
      "Content-Length": stat.size,
      "Cache-Control": "private, max-age=300",
    });
    fs.createReadStream(fullPath).pipe(res);
  } catch {
    res.writeHead(500); res.end("Read error");
  }
}

function apiDocsMkdir(docsDir, name) {
  const validated = validateDocsDir(docsDir);
  if (!validated) return { error: "접근 불가 경로" };
  if (!name || !/^[a-zA-Z0-9가-힣_\-. /]+$/.test(name)) return { error: "잘못된 폴더명" };
  if (name.includes("..")) return { error: "잘못된 폴더명" };

  const target = path.join(validated, name);
  if (!target.startsWith(validated)) return { error: "접근 불가 경로" };
  if (fs.existsSync(target)) return { error: "이미 존재" };

  try {
    fs.mkdirSync(target, { recursive: true });
    return { ok: true, path: name };
  } catch {
    return { error: "폴더 생성 실패" };
  }
}

function parseMultipart(req) {
  return new Promise((resolve, reject) => {
    const contentType = req.headers["content-type"] || "";
    const match = contentType.match(/boundary=(.+)/);
    if (!match) return reject(new Error("No boundary"));
    const boundaryBuf = Buffer.from("--" + match[1]);
    const CRLF2 = Buffer.from("\r\n\r\n");
    const CRLF = Buffer.from("\r\n");

    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const buf = Buffer.concat(chunks);
      const parts = {};

      let pos = 0;
      while (pos < buf.length) {
        const bStart = buf.indexOf(boundaryBuf, pos);
        if (bStart === -1) break;
        const afterBoundary = bStart + boundaryBuf.length;
        // Check for closing boundary (--)
        if (buf[afterBoundary] === 0x2D && buf[afterBoundary + 1] === 0x2D) break;
        const headerStart = afterBoundary + 2; // skip \r\n after boundary
        const headerEnd = buf.indexOf(CRLF2, headerStart);
        if (headerEnd === -1) break;
        const headerStr = buf.slice(headerStart, headerEnd).toString("utf-8");
        const bodyStart = headerEnd + 4;
        const nextBoundary = buf.indexOf(boundaryBuf, bodyStart);
        const bodyEnd = nextBoundary !== -1 ? nextBoundary - 2 : buf.length; // -2 for \r\n before boundary
        const bodyBuf = buf.slice(bodyStart, bodyEnd);

        const nameMatch = headerStr.match(/name="([^"]+)"/);
        if (!nameMatch) { pos = nextBoundary !== -1 ? nextBoundary : buf.length; continue; }
        const name = nameMatch[1];
        const fileMatch = headerStr.match(/filename="([^"]*)"/) || headerStr.match(/filename\*=UTF-8''(.+)/);
        if (fileMatch) {
          let filename = fileMatch[1];
          // RFC 5987 decoding
          if (headerStr.includes("filename*=")) filename = decodeURIComponent(filename);
          parts[name] = { filename, data: bodyBuf };
        } else {
          parts[name] = bodyBuf.toString("utf-8");
        }
        pos = nextBoundary !== -1 ? nextBoundary : buf.length;
      }
      resolve(parts);
    });
    req.on("error", reject);
  });
}

function parseMultipartStreaming(req, maxFileSize) {
  return new Promise((resolve, reject) => {
    const contentType = req.headers["content-type"] || "";
    const bMatch = contentType.match(/boundary=(?:"([^"]+)"|([^\s;]+))/);
    if (!bMatch) return reject(new Error("No boundary"));
    const boundary = (bMatch[1] || bMatch[2]).trim();
    const boundaryBuf = Buffer.from("--" + boundary);
    const DELIM = Buffer.from("\r\n--" + boundary);
    const HEADER_END = Buffer.from("\r\n\r\n");
    const HOLD_BACK = DELIM.length + 4;

    const fields = {};
    let fileFilename = null;
    let fileSize = 0;
    let tempPath = null;
    let writeStream = null;
    let fileStarted = false;
    let done = false;
    let buf = Buffer.alloc(0);
    let held = Buffer.alloc(0);

    function cleanup() {
      if (writeStream) { try { writeStream.destroy(); } catch {} writeStream = null; }
      if (tempPath) { try { fs.unlinkSync(tempPath); } catch {} tempPath = null; }
    }
    function fail(msg) {
      if (done) return;
      done = true; cleanup(); reject(new Error(msg));
      try { req.destroy(); } catch {}
    }
    function flushToFile(data) {
      if (done || !writeStream) return;
      fileSize += data.length;
      if (fileSize > maxFileSize) { fail(`${Math.round(maxFileSize / (1024 * 1024))}MB 초과`); return; }
      writeStream.write(data);
    }
    function parseFieldParts(segment) {
      let pos = 0;
      while (pos < segment.length) {
        const bStart = segment.indexOf(boundaryBuf, pos);
        if (bStart === -1) break;
        const afterB = bStart + boundaryBuf.length;
        if (afterB >= segment.length) break;
        if (segment[afterB] === 0x2D && segment[afterB + 1] === 0x2D) break;
        const hStart = afterB + 2;
        const hEnd = segment.indexOf(HEADER_END, hStart);
        if (hEnd === -1) break;
        const header = segment.slice(hStart, hEnd).toString("utf-8");
        if (header.includes("filename")) { pos = segment.indexOf(boundaryBuf, hEnd); if (pos === -1) break; continue; }
        const bdy = hEnd + 4;
        const nextB = segment.indexOf(boundaryBuf, bdy);
        const bdyEnd = nextB !== -1 ? nextB - 2 : segment.length;
        const nm = header.match(/name="([^"]+)"/);
        if (nm) fields[nm[1]] = segment.slice(bdy, bdyEnd).toString("utf-8");
        pos = nextB !== -1 ? nextB : segment.length;
      }
    }

    req.on("data", (chunk) => {
      if (done) return;
      if (!fileStarted) {
        buf = Buffer.concat([buf, chunk]);
        if (buf.length > 2 * 1024 * 1024) { fail("요청 헤더가 너무 큼"); return; }
        const fnIdx = buf.indexOf(Buffer.from("filename"));
        if (fnIdx === -1) return;
        const hEnd = buf.indexOf(HEADER_END, fnIdx);
        if (hEnd === -1) return;
        const hdr = buf.slice(fnIdx, hEnd).toString("utf-8");
        const m1 = hdr.match(/filename\*=UTF-8''([^\s;\r]+)/);
        const m2 = hdr.match(/filename="([^"]*)"/);
        fileFilename = m1 ? decodeURIComponent(m1[1]) : (m2 ? m2[1] : "unknown");
        parseFieldParts(buf.slice(0, hEnd + 4));
        tempPath = path.join(os.tmpdir(), `pv-upload-${Date.now()}-${Math.random().toString(36).slice(2)}`);
        writeStream = fs.createWriteStream(tempPath);
        writeStream.on("error", (e) => fail("디스크 쓰기 실패: " + e.message));
        fileStarted = true;
        held = buf.slice(hEnd + 4);
        buf = null;
        if (held.length > HOLD_BACK) { flushToFile(held.slice(0, held.length - HOLD_BACK)); held = held.slice(held.length - HOLD_BACK); }
        return;
      }
      held = Buffer.concat([held, chunk]);
      if (held.length > HOLD_BACK) { flushToFile(held.slice(0, held.length - HOLD_BACK)); held = held.slice(held.length - HOLD_BACK); }
    });

    req.on("end", () => {
      if (done) return;
      if (!fileStarted) { parseFieldParts(buf); resolve({ fields, file: null }); return; }
      const dIdx = held.indexOf(DELIM);
      if (dIdx > 0) flushToFile(held.slice(0, dIdx));
      else if (dIdx === -1 && held.length > 0) flushToFile(held);
      if (done) return;
      writeStream.end(() => { resolve({ fields, file: { filename: fileFilename, tempPath, size: fileSize } }); });
    });
    req.on("error", (e) => fail("네트워크 오류: " + e.message));
  });
}

// ─── Auth ────────────────────────────────────────────

// 세션 스토어 (파일 영속 — 재시작 후에도 유지)
const SESSION_FILE = path.join(CONFIG_DIR, "portal-sessions.json");
const SESSION_MAX_AGE = 86400 * 7; // 7일
const sessions = new Map();

function loadSessions() {
  try {
    const raw = JSON.parse(fs.readFileSync(SESSION_FILE, "utf-8"));
    const now = Date.now();
    for (const [k, v] of Object.entries(raw)) {
      if (v.expiresAt > now) sessions.set(k, v);
    }
  } catch {}
}
function saveSessions() {
  try {
    const obj = {};
    for (const [k, v] of sessions) obj[k] = v;
    fs.writeFileSync(SESSION_FILE, JSON.stringify(obj), { mode: 0o600 });
  } catch {}
}
loadSessions();

function generateSessionToken() {
  return crypto.randomBytes(32).toString("hex");
}

function base64url(data) {
  const buf = typeof data === "string" ? Buffer.from(data) : data;
  return buf.toString("base64url");
}

function base64urlDecode(str) {
  return Buffer.from(str, "base64url").toString("utf-8");
}

function verifyJwt(token, secret) {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [header, payload, signature] = parts;
  const expected = base64url(
    crypto.createHmac("sha256", secret).update(`${header}.${payload}`).digest()
  );
  if (signature.length !== expected.length) return null;
  if (!crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) return null;
  try {
    const decoded = JSON.parse(base64urlDecode(payload));
    const now = Math.floor(Date.now() / 1000);
    if (decoded.exp < now) return null;
    return decoded;
  } catch { return null; }
}

function parseCookies(req) {
  const header = req.headers.cookie || "";
  const cookies = {};
  header.split(";").forEach(pair => {
    const [k, ...v] = pair.trim().split("=");
    if (k) cookies[k] = v.join("=");
  });
  return cookies;
}

function isTunnelRequest(req) {
  return !!req.headers["cf-connecting-ip"];
}

// ── 클라우드 모드 (공용 클라우드 데몬 호스트 전용) ──
// PV_CLOUD_MODE=1 로 실행될 때만 활성. 고객 맥미니에선 env 가 없어 완전 무변화.
const CLOUD_MODE = process.env.PV_CLOUD_MODE === "1";
const CLOUD_HOST_KEY = process.env.PV_CLOUD_HOST_KEY || "";
const CLOUD_USERS_ROOT = process.env.PV_USERS_ROOT || "/srv/pv/users";

// ── 클라우드 회의 지원 ──
// 회의 데이터(오디오/메타)는 유저별로 <usersRoot>/<uid>/meetings 에 격리 저장하고,
// 회의록 트리거(봇 호출)는 로스터에서 받은 그 유저의 api_key 로 나간다.
// 유저 식별은 프록시가 검증한 dir(본인 워크스페이스 강제)에서 uid 를 파싱 — 별도
// 헤더 없이 기존 프록시 보안 모델에 편승한다.

function cloudApiUrl() {
  if (process.env.PV_API_URL) return process.env.PV_API_URL;
  try {
    const c = JSON.parse(fs.readFileSync(process.env.PV_CLOUD_CONFIG || "/etc/pv-cloud/config.json", "utf-8"));
    if (c.api_url) return c.api_url;
  } catch { /* fall through */ }
  return "https://www.peter-voice.site";
}

// 로스터(userId→apiKey) 인메모리 캐시. 5분 TTL + 미스 시 갱신.
let meetingRoster = { at: 0, users: new Map() };
async function cloudUserApiKey(uid) {
  if (!CLOUD_MODE || !CLOUD_HOST_KEY) return null;
  const key = String(uid);
  const stale = Date.now() - meetingRoster.at > 5 * 60 * 1000;
  if (stale || !meetingRoster.users.has(key)) {
    try {
      const res = await fetch(`${cloudApiUrl()}/api/cloud/roster`, { headers: { "X-Host-Key": CLOUD_HOST_KEY } });
      if (res.ok) {
        const d = await res.json();
        const m = new Map();
        for (const u of d.users || []) if (u.userId != null && u.apiKey) m.set(String(u.userId), u.apiKey);
        meetingRoster = { at: Date.now(), users: m };
      }
    } catch { /* 로스터 일시 장애 — 기존(스테일) 캐시 유지 */ }
  }
  return meetingRoster.users.get(key) || null;
}

/** 검증된 dir(유저 워크스페이스 하위)에서 uid 추출. 클라우드 모드 전용. */
function cloudUidFromDir(validatedDir) {
  const root = path.resolve(CLOUD_USERS_ROOT);
  const rel = path.relative(root, path.resolve(validatedDir));
  const uid = rel.split(path.sep)[0];
  return uid && uid !== ".." && !uid.startsWith(".") ? uid : null;
}

/**
 * 회의 엔드포인트용 컨텍스트: 회의 저장 베이스(configDir)와 봇 호출용 config.
 * 셀프호스트: 기존 그대로 (CONFIG_DIR + config.json).
 * 클라우드: dir 필수 → uid 파싱 → <usersRoot>/<uid> 베이스 + 유저 api_key.
 * 실패 시 { error } 반환.
 */
async function meetingCtx(dirField) {
  if (!CLOUD_MODE) {
    return { configDir: CONFIG_DIR, config: loadConfig(), uid: null, docsDir: dirField ? validateDocsDir(dirField) : null };
  }
  if (!dirField) return { error: "dir 필요 (클라우드 회의는 프로젝트 경로 필수)" };
  const validated = validateDocsDir(dirField);
  if (!validated) return { error: "접근 불가 경로" };
  const uid = cloudUidFromDir(validated);
  if (!uid) return { error: "접근 불가 경로" };
  const apiKey = await cloudUserApiKey(uid);
  return {
    configDir: path.join(path.resolve(CLOUD_USERS_ROOT), uid),
    config: { api_url: cloudApiUrl(), api_key: apiKey },
    uid,
    docsDir: validated,
  };
}

/** 스윕/자동회의록 대상: 셀프호스트 1건 or 클라우드 유저별(meetings 폴더 있는 것만). */
function meetingSweepTargets() {
  if (!CLOUD_MODE) return [{ configDir: CONFIG_DIR, uid: null }];
  const root = path.resolve(CLOUD_USERS_ROOT);
  try {
    return fs.readdirSync(root, { withFileTypes: true })
      .filter((e) => e.isDirectory() && !e.name.startsWith("."))
      .filter((e) => fs.existsSync(path.join(root, e.name, "meetings")))
      .map((e) => ({ configDir: path.join(root, e.name), uid: e.name }));
  } catch { return []; }
}

// ── 브라우저 세션 인계 (CDP 브리지) ──
// 유저 브라우저(chromium)의 CDP 에 붙어 화면 프레임과 입력을 중계한다.
// 클라우드: 컨테이너 CDP 가 호스트 127.0.0.1:(19000+uid) 로 퍼블리시됨. 셀프호스팅: 9222.
// 자격증명 위생: 입력 이벤트 내용과 프레임은 절대 로깅하지 않는다.
const CDP_PORT_BASE = 19000;
const cdpSessions = new Map(); // port -> session

function cdpPortForDir(dir) {
  if (!CLOUD_MODE) return 9222;
  if (!dir) return null;
  const root = path.resolve(CLOUD_USERS_ROOT);
  const resolved = path.resolve(dir);
  if (!resolved.startsWith(root + path.sep)) return null;
  const uid = parseInt(resolved.slice(root.length + 1).split(path.sep)[0], 10);
  if (!Number.isInteger(uid) || uid <= 0 || uid > 100000) return null;
  return CDP_PORT_BASE + uid;
}

function cdpHttpJson(port, pathName) {
  return new Promise((resolve, reject) => {
    const r = http.get({ host: "127.0.0.1", port, path: pathName, timeout: 3000 }, (resp) => {
      let b = "";
      resp.on("data", c => b += c);
      resp.on("end", () => { try { resolve(JSON.parse(b)); } catch (e) { reject(e); } });
    });
    r.on("timeout", () => r.destroy(new Error("cdp timeout")));
    r.on("error", reject);
  });
}

function cdpHostOf(u) {
  try { return new URL(u).host; } catch { return ""; }
}

// CDP 다운 시 브라우저 셀프힐 — 9222(맥 설치형/컨테이너 내부)에서만.
// 클라우드 호스트 포탈(19000+uid)은 컨테이너 안에서 떠야 하므로 여기서 스폰하면 안 된다
// (그쪽은 데몬 HandoffThread 가 keep-alive 담당).
const browserStartAttempts = new Map(); // port → 마지막 시도 ts (30초 스로틀)
function maybeStartBrowser(port) {
  if (port !== 9222) return false;
  const now = Date.now();
  if (now - (browserStartAttempts.get(port) || 0) < 30000) return true; // 이미 기동 중
  browserStartAttempts.set(port, now);
  const script = [
    path.join(os.homedir(), ".claude", "skills", "browser-handoff", "scripts", "start-browser.sh"),
    "/srv/pv/shared/skills/browser-handoff/scripts/start-browser.sh",
  ].find(p => { try { return fs.existsSync(p); } catch { return false; } });
  if (!script) return false;
  try {
    const child = spawn("bash", [script], {
      env: { ...process.env, PV_CDP_PORT: String(port) },
      detached: true,
      stdio: "ignore",
    });
    child.unref();
    console.log(`[browser] CDP ${port} down — start-browser.sh spawned for self-heal`);
    return true;
  } catch {
    return false;
  }
}

async function cdpConnect(port, matchUrl) {
  // matchUrl(인계의 대상 URL)이 있으면 같은 호스트의 탭을 우선 선택 — 여러 탭 중
  // 엉뚱한 탭을 스트리밍하던 문제의 수정 (2026-07-30 포트원 가입 인계에서 실측)
  const wantHost = matchUrl ? cdpHostOf(matchUrl) : "";
  const existing = cdpSessions.get(port);
  if (existing && existing.ws.readyState === 1 && !wantHost) {
    existing.lastUsed = Date.now();
    return existing;
  }
  if (!WSClient) throw new Error("ws module not available");
  const targets = await cdpHttpJson(port, "/json/list");
  const pages = (targets || []).filter(t => t.type === "page" && !/^(devtools|chrome-extension):/.test(t.url || ""));
  if (!pages.length) throw new Error("no page target");
  let target = wantHost ? pages.find(t => cdpHostOf(t.url) === wantHost) : null;
  if (!target && existing && existing.ws.readyState === 1) {
    // 매칭 탭이 없으면(예: 작업 중 다른 호스트로 리다이렉트) 붙어 있던 탭을 유지 — 탭 점프 방지
    existing.lastUsed = Date.now();
    return existing;
  }
  if (!target) target = pages[0];
  if (existing && existing.ws.readyState === 1) {
    if (existing.targetId === target.id) {
      existing.lastUsed = Date.now();
      return existing;
    }
    try { existing.ws.close(); } catch {}
    cdpSessions.delete(port);
  } else if (existing) {
    cdpSessions.delete(port);
  }
  const session = await new Promise((resolve, reject) => {
    const ws = new WSClient(target.webSocketDebuggerUrl, { perMessageDeflate: false });
    const s = { ws, port, targetId: target.id, nextId: 1, pending: new Map(), lastUsed: Date.now() };
    const to = setTimeout(() => { ws.terminate(); reject(new Error("cdp ws timeout")); }, 5000);
    ws.on("open", () => { clearTimeout(to); resolve(s); });
    ws.on("error", (e) => { clearTimeout(to); reject(e); });
    ws.on("message", (raw) => {
      let msg;
      try { msg = JSON.parse(raw); } catch { return; }
      if (msg.id && s.pending.has(msg.id)) {
        const p = s.pending.get(msg.id);
        s.pending.delete(msg.id);
        clearTimeout(p.timer);
        if (msg.error) p.rej(new Error(msg.error.message || "cdp error"));
        else p.res(msg.result);
      }
    });
    ws.on("close", () => {
      for (const p of s.pending.values()) { clearTimeout(p.timer); p.rej(new Error("cdp closed")); }
      s.pending.clear();
      if (cdpSessions.get(port) === s) cdpSessions.delete(port);
    });
  });
  // 헤드리스 크롬은 스스로를 "포커스 없는 창"으로 여겨 일부 사이트가 입력을 무시한다
  // (네이버 로그인에서 실측) — 포커스 에뮬레이션을 켠다. 실패해도 치명적이지 않음.
  try { await cdpSend(session, "Emulation.setFocusEmulationEnabled", { enabled: true }); } catch {}
  cdpSessions.set(port, session);
  return session;
}

function cdpSend(session, method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = session.nextId++;
    const timer = setTimeout(() => {
      session.pending.delete(id);
      reject(new Error("cdp call timeout: " + method));
    }, 8000);
    session.pending.set(id, { res: resolve, rej: reject, timer });
    session.lastUsed = Date.now();
    try { session.ws.send(JSON.stringify({ id, method, params })); }
    catch (e) { clearTimeout(timer); session.pending.delete(id); reject(e); }
  });
}

// 유휴 CDP 연결 정리 (5분 미사용 시 닫음)
setInterval(() => {
  const now = Date.now();
  for (const [port, s] of cdpSessions) {
    if (now - s.lastUsed > 5 * 60 * 1000) {
      try { s.ws.close(); } catch {}
      cdpSessions.delete(port);
    }
  }
}, 60 * 1000);

const CDP_KEY_MAP = {
  Enter: { windowsVirtualKeyCode: 13, code: "Enter", key: "Enter", text: "\r" },
  Tab: { windowsVirtualKeyCode: 9, code: "Tab", key: "Tab" },
  Backspace: { windowsVirtualKeyCode: 8, code: "Backspace", key: "Backspace" },
  Escape: { windowsVirtualKeyCode: 27, code: "Escape", key: "Escape" },
  Delete: { windowsVirtualKeyCode: 46, code: "Delete", key: "Delete" },
  ArrowLeft: { windowsVirtualKeyCode: 37, code: "ArrowLeft", key: "ArrowLeft" },
  ArrowUp: { windowsVirtualKeyCode: 38, code: "ArrowUp", key: "ArrowUp" },
  ArrowRight: { windowsVirtualKeyCode: 39, code: "ArrowRight", key: "ArrowRight" },
  ArrowDown: { windowsVirtualKeyCode: 40, code: "ArrowDown", key: "ArrowDown" },
};

async function cdpDispatchEvent(session, ev) {
  if (!ev || typeof ev !== "object") return;
  const x = Math.round(Number(ev.x) || 0);
  const y = Math.round(Number(ev.y) || 0);
  switch (ev.t) {
    case "click": {
      const count = Math.min(3, Math.max(1, parseInt(ev.count) || 1));
      const base = { x, y, button: "left", clickCount: count, pointerType: "mouse" };
      await cdpSend(session, "Input.dispatchMouseEvent", { type: "mousePressed", ...base });
      await cdpSend(session, "Input.dispatchMouseEvent", { type: "mouseReleased", ...base });
      break;
    }
    case "move":
      await cdpSend(session, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
      break;
    case "wheel":
      await cdpSend(session, "Input.dispatchMouseEvent", {
        type: "mouseWheel", x, y,
        deltaX: Math.round(Number(ev.dx2) || 0), deltaY: Math.round(Number(ev.dy) || 0),
      });
      break;
    case "text":
      if (typeof ev.text === "string" && ev.text.length > 0 && ev.text.length <= 500) {
        await cdpSend(session, "Input.insertText", { text: ev.text });
      }
      break;
    case "key": {
      const k = CDP_KEY_MAP[ev.key];
      if (!k) break;
      await cdpSend(session, "Input.dispatchKeyEvent", { type: "keyDown", ...k });
      await cdpSend(session, "Input.dispatchKeyEvent", {
        type: "keyUp", windowsVirtualKeyCode: k.windowsVirtualKeyCode, code: k.code, key: k.key,
      });
      break;
    }
    case "nav":
      if (typeof ev.url === "string" && /^https?:\/\//.test(ev.url)) {
        await cdpSend(session, "Page.navigate", { url: ev.url.slice(0, 2000) });
      }
      break;
  }
}

// JWT scope 권한 검사. scope="share"(공유 링크, 7일)는 공유 뷰어 경로 +
// (토큰에 dir 있으면) 그 docs 디렉토리만 허용 — 공유 링크 수신자가 토큰을 뽑아
// 포탈 API 전체(회의 목록·라벨·문서 삭제 등)를 호출하던 구멍 차단.
// scope="portal"(5분) 또는 scope 없음(구버전 토큰)은 전체 허용.
// TODO(2026-08-19 이후): 구버전 공유 링크(7일 TTL)가 전부 만료되면 쿼리 토큰의
// scope 부재를 거부로 전환 (docs/plans/meeting-weakness-fix-plan.md 2-A 3단계).
const SHARE_SCOPE_PATHS = new Set(["/share", "/api/docs/file"]);
function jwtScopeAllows(payload, req) {
  if (!payload || payload.scope !== "share") return true;
  const urlObj = new URL(req.url, "http://localhost");
  if (!SHARE_SCOPE_PATHS.has(urlObj.pathname)) return false;
  if (payload.dir) {
    const qDir = urlObj.searchParams.get("dir");
    if (!qDir || path.resolve(qDir) !== path.resolve(payload.dir)) return false;
  }
  return true;
}

function verifyAuth(req) {
  if (CLOUD_MODE) {
    // 클라우드: host_key 헤더만 인정 (웹 프록시 전용).
    // 로컬호스트 바이패스 금지 — 호스트 위 pv유저 프로세스의 타 유저 접근 차단.
    return !!CLOUD_HOST_KEY && req.headers["x-host-key"] === CLOUD_HOST_KEY;
  }
  // 로컬 요청은 인증 불필요
  if (!isTunnelRequest(req)) return true;

  const config = loadConfig();
  const apiKey = config.api_key;
  if (!apiKey) return false;

  // 1. 세션 쿠키 인증 (접속 시마다 만료 연장)
  const cookies = parseCookies(req);
  const sessionToken = cookies["pv_session"];
  if (sessionToken && sessions.has(sessionToken)) {
    const session = sessions.get(sessionToken);
    if (session.expiresAt > Date.now()) {
      session.expiresAt = Date.now() + SESSION_MAX_AGE * 1000;
      saveSessions();
      return true;
    }
    sessions.delete(sessionToken); // 만료된 세션 정리
  }

  // 2. X-Api-Key 헤더 (데몬 호출용)
  const xApiKey = req.headers["x-api-key"];
  if (xApiKey && xApiKey === apiKey) return true;

  // 3. Authorization: Bearer JWT (브라우저 직접 통신용)
  const authHeader = req.headers["authorization"];
  if (authHeader && authHeader.startsWith("Bearer ")) {
    const jwt = authHeader.slice(7);
    // raw api_key 일치도 허용 (하위 호환)
    if (jwt === apiKey) return true;
    // JWT 검증 + scope 권한 (share 토큰은 공유 뷰어 경로만)
    const bearerPayload = verifyJwt(jwt, apiKey);
    if (bearerPayload && jwtScopeAllows(bearerPayload, req)) return true;
  }

  // 4. Query parameter token (공유 링크용)
  const urlObj = new URL(req.url, `http://localhost`);
  const queryToken = urlObj.searchParams.get("token");
  const queryPayload = queryToken ? verifyJwt(queryToken, apiKey) : null;
  if (queryPayload && jwtScopeAllows(queryPayload, req)) return true;

  return false;
}

// JWT auth → 세션 쿠키 발급, 302 redirect
function handleAuthCallback(req, res, url) {
  const authToken = url.searchParams.get("auth");
  if (!authToken) return false;

  const config = loadConfig();
  const apiKey = config.api_key;
  if (!apiKey) {
    res.writeHead(401, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "API key not configured" }));
    return true;
  }

  const payload = verifyJwt(authToken, apiKey);
  if (!payload) {
    res.writeHead(401, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "유효하지 않거나 만료된 토큰" }));
    return true;
  }

  // 세션 생성
  const sessionToken = generateSessionToken();
  sessions.set(sessionToken, {
    createdAt: Date.now(),
    expiresAt: Date.now() + SESSION_MAX_AGE * 1000,
  });
  saveSessions();

  // 쿠키 세팅 + 깨끗한 URL로 리다이렉트
  const redirectUrl = url.pathname || "/";
  res.writeHead(302, {
    Location: redirectUrl,
    "Set-Cookie": `pv_session=${sessionToken}; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_MAX_AGE}; Path=/`,
  });
  res.end();
  return true;
}

// 만료 세션 정리 (1시간마다)
setInterval(() => {
  const now = Date.now();
  let changed = false;
  for (const [token, session] of sessions) {
    if (session.expiresAt <= now) { sessions.delete(token); changed = true; }
  }
  if (changed) saveSessions();
}, 3600000);

// ─── Docs Search ────────────────────────────────────

function apiDocsSearch(docsDir, query) {
  const validated = validateDocsDir(docsDir);
  if (!validated) return { error: "접근 불가 경로" };
  if (!fs.existsSync(validated)) return { results: [], total: 0 };
  if (!query || !query.trim()) return { results: [], total: 0 };

  const q = query.trim().toLowerCase();
  const TEXT_EXTS = new Set([
    ".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".xml",
    ".sh", ".bash", ".zsh", ".sql", ".env", ".ini", ".cfg",
    ".rs", ".go", ".java", ".c", ".cpp", ".h", ".rb", ".php",
    ".swift", ".kt", ".r", ".lua", ".pl", ".ex", ".exs",
  ]);
  const MAX_FILE_SIZE = 1024 * 1024;
  const MAX_RESULTS = 100;
  const SNIPPET_CONTEXT = 60;

  const results = [];

  function scan(dir, prefix) {
    if (results.length >= MAX_RESULTS) return;
    try {
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      for (const entry of entries) {
        if (results.length >= MAX_RESULTS) return;
        if (entry.name.startsWith(".")) continue;
        const fullPath = path.join(dir, entry.name);
        const relPath = prefix ? `${prefix}/${entry.name}` : entry.name;

        if (entry.isDirectory()) {
          scan(fullPath, relPath);
          continue;
        }

        const ext = path.extname(entry.name).toLowerCase();
        const titleMatch = entry.name.toLowerCase().includes(q);
        const canReadContent = TEXT_EXTS.has(ext);
        const contentMatches = [];

        if (canReadContent) {
          try {
            const stat = fs.statSync(fullPath);
            if (stat.size <= MAX_FILE_SIZE) {
              const content = fs.readFileSync(fullPath, "utf-8");
              const lines = content.split("\n");
              for (let i = 0; i < lines.length; i++) {
                if (contentMatches.length >= 3) break;
                const idx = lines[i].toLowerCase().indexOf(q);
                if (idx !== -1) {
                  const line = lines[i];
                  const start = Math.max(0, idx - SNIPPET_CONTEXT);
                  const end = Math.min(line.length, idx + q.length + SNIPPET_CONTEXT);
                  contentMatches.push({
                    line: i + 1,
                    text: (start > 0 ? "..." : "") + line.slice(start, end) + (end < line.length ? "..." : ""),
                  });
                }
              }
            }
          } catch { /* skip unreadable files */ }
        }

        if (titleMatch || contentMatches.length > 0) {
          let fileType = "file";
          const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"]);
          if (ext === ".md") fileType = "doc";
          else if (IMAGE_EXTS.has(ext)) fileType = "image";
          else if (TEXT_EXTS.has(ext)) fileType = "text";

          const stat = fs.statSync(fullPath);
          results.push({
            title: ext === ".md" ? entry.name.replace(/\.md$/, "") : entry.name,
            file_path: relPath,
            type: fileType,
            title_match: titleMatch,
            matches: contentMatches,
            modified: stat.mtime.toISOString(),
          });
        }
      }
    } catch { /* skip unreadable dirs */ }
  }

  scan(validated, "");
  results.sort((a, b) => {
    if (a.title_match !== b.title_match) return a.title_match ? -1 : 1;
    return new Date(b.modified).getTime() - new Date(a.modified).getTime();
  });

  return { results, total: results.length };
}

// ─── Server ──────────────────────────────────────────

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const pathname = url.pathname;

  // CORS — 특정 origin만 허용 (브라우저 직접 통신)
  // 중앙(www/canary) + 모든 자체호스팅 인스턴스({username}.peter-voice.site)를 허용.
  // peter-voice.site 존은 본사 소유라 서브도메인 전체 허용이 안전.
  const ALLOWED_ORIGINS = ["https://canary.peter-voice.site", "https://www.peter-voice.site", "http://localhost:3001"];
  const PV_SUBDOMAIN = /^https:\/\/[a-z0-9-]+\.peter-voice\.site$/;
  const origin = req.headers.origin;
  if (origin && (ALLOWED_ORIGINS.includes(origin) || PV_SUBDOMAIN.test(origin))) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Access-Control-Allow-Credentials", "true");
  } else if (!origin) {
    // origin 없는 요청 (같은 도메인, curl 등) 허용
    res.setHeader("Access-Control-Allow-Origin", "*");
  }
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Api-Key, Authorization");
  res.setHeader("Access-Control-Allow-Private-Network", "true");
  if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }

  // JSON helper
  const json = (data, status = 200) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(data));
  };

  // JWT auth callback: ?auth=JWT → 세션 쿠키 발급 후 리다이렉트
  if (url.searchParams.has("auth")) {
    if (handleAuthCallback(req, res, url)) return;
  }

  // Auth check: 모든 경로에 인증 적용 (localhost는 스킵)
  if (!verifyAuth(req)) {
    return json({ error: "인증 필요" }, 401);
  }

  // 클라우드 모드: docs API + 브라우저 인계 + 회의 API만 노출 (공유/사이트/기타 로컬 기능 차단)
  // 회의 API 는 핸들러 내부(meetingCtx)에서 dir 필수 + 유저별 격리를 강제한다.
  if (CLOUD_MODE && !pathname.startsWith("/api/docs") && !pathname.startsWith("/api/browser")
      && !pathname.startsWith("/api/meetings") && pathname !== "/api/graph") {
    return json({ error: "cloud mode: not available" }, 404);
  }
  // 클라우드 모드: 쿼리 dir 중앙 검증 (일부 핸들러가 개별 검증을 생략하므로 초크포인트 필수)
  if (CLOUD_MODE) {
    const qDir = url.searchParams.get("dir");
    if (qDir && !validateDocsDir(qDir)) {
      return json({ error: "접근 불가 경로" }, 403);
    }
  }

  // Read body helper
  const readBody = () => new Promise((resolve) => {
    let body = "";
    req.on("data", c => body += c);
    req.on("end", () => {
      try {
        const parsed = JSON.parse(body);
        // 클라우드 모드: body.dir 도 중앙 검증 — 무효면 dir 제거 (핸들러가 400 처리)
        if (CLOUD_MODE && parsed && parsed.dir && !validateDocsDir(parsed.dir)) {
          delete parsed.dir;
        }
        resolve(parsed);
      } catch { resolve({}); }
    });
  });

  // Routes
  if (pathname === "/" && req.method === "GET") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(renderHTML(req));
  }
  else if (pathname === "/api/terminal/capture" && req.method === "GET") {
    // 터미널 화면(+스크롤백) 텍스트를 평문으로 반환 — 웹에서 선택/복사용
    // (claude TUI는 화면을 계속 다시 그려 xterm 선택이 지워지므로, 캡처 방식이 안정적)
    // 인증: X-Api-Key 헤더(서버 간) 또는 단기 JWT(?token=, 브라우저용).
    // raw 키를 URL 쿼리(?key=)로 받는 방식은 로그 잔존 위험으로 제거 (2026-08-06 보안 점검).
    const capCfg = loadConfig();
    const capHeaderKey = req.headers["x-api-key"];
    const capToken = url.searchParams.get("token");
    const capAuthed = (capHeaderKey && capHeaderKey === capCfg.api_key) ||
      (capToken && verifyJwt(capToken, capCfg.api_key));
    if (!capAuthed) { res.writeHead(401); res.end("Unauthorized"); return; }
    const project = url.searchParams.get("project") || "general";
    const branch = url.searchParams.get("branch") || null;
    const mode = url.searchParams.get("mode") === "shell" ? "shell" : "claude";
    const sessionKey = mode === "shell" ? "__shell__" : getSessionKey(project, branch);
    const cap = spawnSync(TMUX_CMD, ["capture-pane", "-p", "-S", "-2000", "-t", sessionKey], { env: PORTAL_ENV, maxBuffer: 8 * 1024 * 1024 });
    if (cap.status !== 0) {
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("(세션을 찾을 수 없습니다)");
      return;
    }
    // 끝부분의 빈 줄 정리
    const text = cap.stdout.toString().replace(/\n+$/, "\n");
    res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache" });
    res.end(text);
  }
  else if (pathname === "/api/sites" && req.method === "GET") {
    json(apiSites());
  }
  else if (pathname === "/api/projects" && req.method === "GET") {
    json(apiProjects());
  }
  else if (pathname === "/api/system" && req.method === "GET") {
    json(apiSystem());
  }
  else if (pathname === "/api/browse" && req.method === "GET") {
    const dir = url.searchParams.get("dir") || "";
    json(apiBrowse(dir));
  }
  else if (pathname.startsWith("/api/logs/") && req.method === "GET") {
    const id = pathname.split("/api/logs/")[1];
    json(apiLogs(decodeURIComponent(id)));
  }
  // Docs API: /api/docs/all — 전체 프로젝트 docs 트리
  else if (pathname === "/api/docs/all" && req.method === "GET") {
    json(apiDocsAll());
  }
  // Docs API: /api/docs/search — 문서 검색 (dir + q 쿼리 파라미터)
  else if (pathname === "/api/docs/search" && req.method === "GET") {
    const dir = url.searchParams.get("dir");
    const q = url.searchParams.get("q");
    if (!dir) return json({ error: "dir 파라미터 필요" }, 400);
    if (!q) return json({ results: [], total: 0 });
    json(apiDocsSearch(dir, q));
  }
  // Docs API: /api/docs — 문서 목록 (dir 쿼리 파라미터)
  else if (pathname === "/api/docs" && req.method === "GET") {
    const dir = url.searchParams.get("dir");
    if (!dir) return json({ error: "dir 파라미터 필요" }, 400);
    json(apiDocsList(dir));
  }
  // Docs API: /api/docs/read — 문서 내용 (dir + path 쿼리 파라미터)
  else if (pathname === "/api/docs/read" && req.method === "GET") {
    const dir = url.searchParams.get("dir");
    const docPath = url.searchParams.get("path");
    if (!dir || !docPath) return json({ error: "dir, path 파라미터 필요" }, 400);
    json(apiDocsRead(dir, docPath));
  }
  // Docs API: /api/docs/file — 바이너리 파일 서빙
  else if (pathname === "/api/docs/file" && req.method === "GET") {
    const dir = url.searchParams.get("dir");
    const filePath = url.searchParams.get("path");
    if (!dir || !filePath) return json({ error: "dir, path 파라미터 필요" }, 400);
    serveDocsFile(res, dir, filePath);
  }
  // Docs API: /api/docs/mkdir — 폴더 생성
  else if (pathname === "/api/docs/mkdir" && req.method === "POST") {
    readBody().then(body => {
      const { dir, name } = body;
      if (!dir || !name) return json({ error: "dir, name 필요" }, 400);
      json(apiDocsMkdir(dir, name));
    });
  }
  // Docs API: /api/docs/upload — 파일 업로드 (스트리밍 + 청크 지원)
  else if (pathname === "/api/docs/upload" && req.method === "POST") {
    const MAX_FILE_SIZE = 500 * 1024 * 1024;
    parseMultipartStreaming(req, MAX_FILE_SIZE).then(({ fields, file }) => {
      if (!fields.dir || !file) return json({ error: "dir, file 필요" }, 400);
      const validated = validateDocsDir(fields.dir);
      if (!validated) { if (file.tempPath) try { fs.unlinkSync(file.tempPath); } catch {} return json({ error: "접근 불가 경로" }, 403); }
      const subpath = fields.path || "";

      // ── 청크 업로드 모드 ──
      if (fields.uploadId) {
        const chunkIdx = parseInt(fields.chunkIndex || "0");
        const totalChunks = parseInt(fields.totalChunks || "1");
        const chunkDir = path.join(os.tmpdir(), "pv-chunks");
        fs.mkdirSync(chunkDir, { recursive: true });
        const chunkFile = path.join(chunkDir, `${fields.uploadId}.part`);
        try {
          if (chunkIdx === 0) fs.copyFileSync(file.tempPath, chunkFile);
          else fs.appendFileSync(chunkFile, fs.readFileSync(file.tempPath));
          fs.unlinkSync(file.tempPath);
          if (chunkIdx < totalChunks - 1) return json({ ok: true, chunk: chunkIdx });
          // 마지막 청크 — 최종 위치로 이동
          const stat = fs.statSync(chunkFile);
          if (stat.size > MAX_FILE_SIZE) { fs.unlinkSync(chunkFile); return json({ error: "500MB 초과" }, 413); }
          const targetDir = subpath ? path.join(validated, path.dirname(subpath)) : validated;
          const fileName = subpath ? path.basename(subpath) : file.filename;
          const targetPath = path.resolve(targetDir, fileName);
          if (!targetPath.startsWith(validated)) { fs.unlinkSync(chunkFile); return json({ error: "접근 불가 경로" }, 403); }
          fs.mkdirSync(targetDir, { recursive: true });
          fs.renameSync(chunkFile, targetPath);
          return json({ ok: true, path: path.relative(validated, targetPath), size: stat.size });
        } catch (e) {
          try { fs.unlinkSync(file.tempPath); } catch {}
          return json({ error: "청크 저장 실패: " + e.message }, 500);
        }
      }

      // ── 일반 업로드 (스트리밍 완료) ──
      const targetDir = subpath ? path.join(validated, path.dirname(subpath)) : validated;
      const fileName = subpath ? path.basename(subpath) : file.filename;
      const targetPath = path.resolve(targetDir, fileName);
      if (!targetPath.startsWith(validated)) { try { fs.unlinkSync(file.tempPath); } catch {} return json({ error: "접근 불가 경로" }, 403); }
      try {
        fs.mkdirSync(targetDir, { recursive: true });
        fs.renameSync(file.tempPath, targetPath);
        json({ ok: true, path: path.relative(validated, targetPath), size: file.size });
      } catch (e) {
        try { fs.unlinkSync(file.tempPath); } catch {}
        json({ error: "저장 실패: " + e.message }, 500);
      }
    }).catch(e => json({ error: "업로드 실패: " + e.message }, 400));
  }
  // ─── Meeting API (회의록 모드) ───────────────────────
  // POST /api/meetings/upload — 청크 업로드(브라우저→로컬). 마지막 청크에서 처리 시작.
  else if (pathname === "/api/meetings/upload" && req.method === "POST") {
    if (!meetingStore || !processMeeting) return json({ error: "meeting 모듈 없음" }, 503);
    const MAX = 1024 * 1024 * 1024; // 1GB (긴 회의 대비)
    parseMultipartStreaming(req, MAX).then(async ({ fields, file }) => {
      const dropTemp = () => { if (file && file.tempPath) { try { fs.unlinkSync(file.tempPath); } catch {} } };
      const meetingId = (fields.meetingId || "").replace(/[^a-zA-Z0-9_-]/g, "");
      if (!meetingId || !file) { dropTemp(); return json({ error: "meetingId, file 필요" }, 400); }

      // 셀프호스트: dir 선택 / 클라우드: dir 필수 → 유저별 저장 격리 + 유저 api_key
      const ctx = await meetingCtx(fields.dir);
      if (ctx.error) { dropTemp(); return json({ error: ctx.error }, 403); }
      const baseDir = ctx.configDir;
      const docsDir = ctx.docsDir;

      // 멱등성: 이미 조립 완료(메타 존재)된 회의면 어떤 청크가 재전송돼도 기존
      // 메타를 돌려준다 — 마지막 청크의 응답 유실 → 재전송이 완성된 오디오를
      // 마지막 조각 하나로 덮어쓰고 처리를 이중 시작하던 경로 차단.
      const existingMeta = meetingStore.readMeta(baseDir, meetingId);
      if (existingMeta) {
        dropTemp();
        return json({ ok: true, meeting: existingMeta, already: true });
      }

      const chunkIdx = parseInt(fields.chunkIndex || "0");
      const totalChunks = parseInt(fields.totalChunks || "1");
      const chunkDir = path.join(os.tmpdir(), "pv-meeting-chunks");
      fs.mkdirSync(chunkDir, { recursive: true });
      // 클라우드: uid 프리픽스 — 다른 유저가 같은 meetingId 로 조립 파일을 건드리지 못하게
      const chunkBase = `${ctx.uid ? ctx.uid + "-" : ""}${meetingId}`;
      const chunkFile = path.join(chunkDir, `${chunkBase}.part`);
      const stateFile = path.join(chunkDir, `${chunkBase}.part.state`);
      const readChunkState = () => { try { return JSON.parse(fs.readFileSync(stateFile, "utf-8")); } catch { return null; } };
      const clearParts = () => { try { fs.unlinkSync(chunkFile); } catch {} try { fs.unlinkSync(stateFile); } catch {} };
      try {
        // 순서 강제 + 중복 무시(멱등): 응답만 유실된 청크가 재전송되어 두 번
        // append 되면 오디오가 손상된다 — 이미 반영된 인덱스는 조용히 성공 처리.
        const next = (readChunkState() || { next: 0 }).next;
        let early = null;
        if (chunkIdx === 0) {
          fs.copyFileSync(file.tempPath, chunkFile); // 0번 = 항상 새 시작(재시작 포함)
          fs.writeFileSync(stateFile, JSON.stringify({ next: 1 }));
        } else if (chunkIdx < next) {
          early = () => json({ ok: true, chunk: chunkIdx, duplicate: true });
        } else if (chunkIdx === next && fs.existsSync(chunkFile)) {
          fs.appendFileSync(chunkFile, fs.readFileSync(file.tempPath));
          fs.writeFileSync(stateFile, JSON.stringify({ next: chunkIdx + 1 }));
        } else {
          // 순서 건너뜀 또는 조립 파일 유실(재시작 등) → 재개/재시작 지점 안내
          const resumeFrom = fs.existsSync(chunkFile) ? next : 0;
          if (resumeFrom === 0) clearParts();
          early = () => json({ error: "청크 순서 불일치", resume_from: resumeFrom }, 409);
        }
        fs.unlinkSync(file.tempPath);
        if (early) return early();
      } catch (e) {
        try { fs.unlinkSync(file.tempPath); } catch {}
        return json({ error: "청크 저장 실패: " + e.message }, 500);
      }
      if (chunkIdx < totalChunks - 1) return json({ ok: true, chunk: chunkIdx });

      // 마지막 청크 → 조립 크기 검증 → 영구 저장 + 메타 생성 + 백그라운드 처리
      const expectedSize = parseInt(fields.totalSize || "0") || 0;
      let assembledSize = 0;
      try { assembledSize = fs.statSync(chunkFile).size; } catch { /* 검증에서 걸림 */ }
      if (expectedSize > 0 && assembledSize !== expectedSize) {
        clearParts();
        return json({ error: `조립 크기 불일치 (${assembledSize} != ${expectedSize})`, restart: true }, 409);
      }

      const now = new Date().toISOString();
      meetingStore.storeAudio(baseDir, meetingId, chunkFile);
      try { fs.unlinkSync(stateFile); } catch {}
      const meta = meetingStore.writeMeta(baseDir, meetingId, {
        id: meetingId,
        project: fields.project || null,
        title: fields.title || "제목 없는 회의",
        duration_sec: parseInt(fields.duration || "0") || null,
        live_transcript: fields.liveTranscript || null,
        // Stored up front so the stuck-meeting sweep can retry processing after
        // a Home Portal restart (previously only recorded on success).
        docs_dir: docsDir || null,
        ...(ctx.uid ? { cloud_uid: ctx.uid } : {}),
        status: "processing",
        created_at: now,
        updated_at: now,
      });

      processMeeting({
        configDir: baseDir,
        config: ctx.config,
        meetingId,
        projectDocsDir: docsDir,
        log: (m) => console.log(m),
      }).catch((e) => console.error("[meeting] process error:", e));

      json({ ok: true, meeting: meta });
    }).catch(e => json({ error: "업로드 실패: " + e.message }, 400));
  }
  // GET /api/meetings/list — 회의 목록(메타). 큰 필드 제외.
  // 클라우드: ?dir=(본인 워크스페이스) 필수 → 그 유저의 회의만 (크로스 테넌트 차단)
  else if (pathname === "/api/meetings/list" && req.method === "GET") {
    if (!meetingStore) return json({ error: "meeting 모듈 없음" }, 503);
    meetingCtx(url.searchParams.get("dir")).then((ctx) => {
      if (ctx.error) return json({ error: ctx.error }, 403);
      const meetings = meetingStore.listMeetings(ctx.configDir).map((m) => {
        const { segments, live_transcript, speaker_samples, ...slim } = m;
        return slim;
      });
      json({ meetings });
    }).catch((e) => json({ error: e.message }, 500));
  }
  // GET /api/meetings/get?id= — 단일 회의 메타 (큰 필드 제외). 클라우드: ?dir= 필수
  else if (pathname === "/api/meetings/get" && req.method === "GET") {
    if (!meetingStore) return json({ error: "meeting 모듈 없음" }, 503);
    meetingCtx(url.searchParams.get("dir")).then((ctx) => {
      if (ctx.error) return json({ error: ctx.error }, 403);
      const id = url.searchParams.get("id");
      const meta = id ? meetingStore.readMeta(ctx.configDir, id) : null;
      if (!meta) return json({ error: "not found" }, 404);
      const { segments, live_transcript, ...slim } = meta;
      json({ meeting: slim });
    }).catch((e) => json({ error: e.message }, 500));
  }
  // POST /api/meetings/label — 화자 이름 부여 + 문서 재작성 (등록 없음). 클라우드: body.dir 필수
  else if (pathname === "/api/meetings/label" && req.method === "POST") {
    if (!meetingStore || !meetingLabel) return json({ error: "meeting 모듈 없음" }, 503);
    readBody().then(async (body) => {
      const { meetingId, labels, dir } = body;
      if (!meetingId || !labels || typeof labels !== "object") return json({ error: "meetingId, labels 필요" }, 400);
      const ctx = await meetingCtx(dir);
      if (ctx.error) return json({ error: ctx.error }, 403);
      try {
        const result = await meetingLabel({ configDir: ctx.configDir, config: ctx.config, meetingId, labels });
        json({ ok: true, ...result });
      } catch (e) {
        json({ error: e.message }, 500);
      }
    }).catch((e) => json({ error: e.message }, 400));
  }
  // Docs API: /api/docs/copy — 파일/폴더 복사 (다른 프로젝트로)
  else if (pathname === "/api/docs/copy" && req.method === "POST") {
    readBody().then(body => {
      const { dir, filePath, targetDir, targetPath: tp } = body;
      if (!dir || !filePath || !targetDir) return json({ error: "dir, filePath, targetDir 필요" }, 400);
      const srcBase = validateDocsDir(dir);
      const dstBase = validateDocsDir(targetDir);
      if (!srcBase || !dstBase) return json({ error: "접근 불가 경로" }, 403);
      const srcFull = path.resolve(srcBase, filePath);
      const dstFull = path.resolve(dstBase, tp || filePath);
      if (!srcFull.startsWith(srcBase) || !dstFull.startsWith(dstBase)) return json({ error: "접근 불가 경로" }, 403);
      if (!fs.existsSync(srcFull)) return json({ error: "원본 없음" }, 404);
      try {
        fs.mkdirSync(path.dirname(dstFull), { recursive: true });
        fs.cpSync(srcFull, dstFull, { recursive: true });
        json({ ok: true, dest: path.relative(dstBase, dstFull) });
      } catch (e) { json({ error: "복사 실패: " + e.message }, 500); }
    }).catch(e => json({ error: e.message }, 400));
  }
  // Docs API: /api/docs/move — 파일/폴더 이동 (다른 프로젝트로)
  else if (pathname === "/api/docs/move" && req.method === "POST") {
    readBody().then(body => {
      const { dir, filePath, targetDir, targetPath: tp } = body;
      if (!dir || !filePath || !targetDir) return json({ error: "dir, filePath, targetDir 필요" }, 400);
      const srcBase = validateDocsDir(dir);
      const dstBase = validateDocsDir(targetDir);
      if (!srcBase || !dstBase) return json({ error: "접근 불가 경로" }, 403);
      const srcFull = path.resolve(srcBase, filePath);
      const dstFull = path.resolve(dstBase, tp || filePath);
      if (!srcFull.startsWith(srcBase) || !dstFull.startsWith(dstBase)) return json({ error: "접근 불가 경로" }, 403);
      if (!fs.existsSync(srcFull)) return json({ error: "원본 없음" }, 404);
      try {
        fs.mkdirSync(path.dirname(dstFull), { recursive: true });
        fs.renameSync(srcFull, dstFull);
        json({ ok: true, dest: path.relative(dstBase, dstFull) });
      } catch (e) { json({ error: "이동 실패: " + e.message }, 500); }
    }).catch(e => json({ error: e.message }, 400));
  }
  // Docs API: /api/docs/delete — 파일/폴더 삭제
  else if (pathname === "/api/docs/delete" && req.method === "POST") {
    readBody().then(body => {
      const { dir, filePath } = body;
      if (!dir) return json({ error: "dir 필요" }, 400);
      const base = validateDocsDir(dir);
      if (!base) return json({ error: "접근 불가 경로" }, 403);
      const full = (filePath === '' || filePath == null) ? base : path.resolve(base, filePath);
      if (full !== base && !full.startsWith(base)) return json({ error: "접근 불가 경로" }, 403);
      if (!fs.existsSync(full)) return json({ error: "파일 없음" }, 404);
      try {
        fs.rmSync(full, { recursive: true, force: true });
        json({ ok: true });
      } catch (e) { json({ error: "삭제 실패: " + e.message }, 500); }
    }).catch(e => json({ error: e.message }, 400));
  }
  // Docs API: /api/docs/write — 텍스트 파일 생성/수정
  else if (pathname === "/api/docs/write" && req.method === "POST") {
    readBody().then(body => {
      const { dir, path: filePath, content } = body;
      if (!dir || !filePath || content == null) return json({ error: "dir, path, content 필요" }, 400);
      const base = validateDocsDir(dir);
      if (!base) return json({ error: "접근 불가 경로" }, 403);
      const full = path.resolve(base, filePath);
      if (!full.startsWith(base)) return json({ error: "접근 불가 경로" }, 403);
      try {
        fs.mkdirSync(path.dirname(full), { recursive: true });
        fs.writeFileSync(full, content, "utf-8");
        json({ ok: true, path: filePath, size: Buffer.byteLength(content, "utf-8") });
      } catch (e) { json({ error: "저장 실패: " + e.message }, 500); }
    }).catch(e => json({ error: e.message }, 400));
  }
  // Graph API: /api/graph — graphify 지식 그래프 HTML 서빙
  else if (pathname === "/api/graph" && req.method === "GET") {
    const dir = url.searchParams.get("dir");
    const type = url.searchParams.get("type") || "html"; // html | report | stats
    if (!dir) return json({ error: "dir 파라미터 필요" }, 400);
    const base = validateDocsDir(dir);
    if (!base) return json({ error: "접근 불가 경로" }, 403);
    const graphDir = path.join(base, "graphify-out");
    if (!fs.existsSync(graphDir)) return json({ error: "그래프 없음" }, 404);

    if (type === "html") {
      const htmlPath = path.join(graphDir, "graph.html");
      if (!fs.existsSync(htmlPath)) return json({ error: "graph.html 없음" }, 404);
      const content = fs.readFileSync(htmlPath);
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Content-Length": content.length, "Cache-Control": "no-cache" });
      res.end(content);
    } else if (type === "report") {
      const reportPath = path.join(graphDir, "GRAPH_REPORT.md");
      if (!fs.existsSync(reportPath)) return json({ error: "GRAPH_REPORT.md 없음" }, 404);
      json({ content: fs.readFileSync(reportPath, "utf-8") });
    } else if (type === "stats") {
      const graphPath = path.join(graphDir, "graph.json");
      if (!fs.existsSync(graphPath)) return json({ error: "graph.json 없음" }, 404);
      try {
        const g = JSON.parse(fs.readFileSync(graphPath, "utf-8"));
        const nodes = g.nodes ? g.nodes.length : 0;
        const edges = (g.links || g.edges || []).length;
        const stat = fs.statSync(graphPath);
        json({ nodes, edges, updated: stat.mtime.toISOString() });
      } catch (e) { json({ error: "파싱 실패" }, 500); }
    } else if (type === "json") {
      const graphPath = path.join(graphDir, "graph.json");
      if (!fs.existsSync(graphPath)) return json({ error: "graph.json 없음" }, 404);
      try {
        const raw = fs.readFileSync(graphPath, "utf-8");
        const g = JSON.parse(raw);
        const nodes = g.nodes || [];
        const links = g.links || g.edges || [];
        const hyperedges = g.hyperedges || (g.graph && g.graph.hyperedges) || [];
        // degree 계산
        const degreeMap = {};
        links.forEach(l => {
          degreeMap[l.source] = (degreeMap[l.source] || 0) + 1;
          degreeMap[l.target] = (degreeMap[l.target] || 0) + 1;
        });
        nodes.forEach(n => { n.degree = degreeMap[n.id] || 0; });
        // Surprising Connections from GRAPH_REPORT.md
        let surprisingConnections = [];
        const reportPath = path.join(graphDir, "GRAPH_REPORT.md");
        if (fs.existsSync(reportPath)) {
          const report = fs.readFileSync(reportPath, "utf-8");
          const scMatch = report.match(/## Surprising Connections.*?\n([\s\S]*?)(?=\n## |\n$)/);
          if (scMatch) {
            const lines = scMatch[1].split("\n").filter(l => l.startsWith("- "));
            for (let i = 0; i < lines.length; i++) {
              const m = lines[i].match(/`([^`]+)`\s+--([^-]+)-->\s+`([^`]+)`\s+\[(\w+)\]/);
              if (m) {
                const detail = lines[i + 1]?.trim() || "";
                surprisingConnections.push({ from: m[1], relation: m[2].trim(), to: m[3], confidence: m[4], detail });
              }
            }
          }
        }
        const stat = fs.statSync(graphPath);
        json({ nodes, links, hyperedges, surprisingConnections, updated: stat.mtime.toISOString() });
      } catch (e) { json({ error: "파싱 ��패: " + e.message }, 500); }
    } else {
      json({ error: "type은 html, report, stats, json 중 하나" }, 400);
    }
  }
  // Graph API: /api/graph/list — graphify가 빌드된 프로젝트 목록
  else if (pathname === "/api/graph/list" && req.method === "GET") {
    const projectsDir = path.join(os.homedir(), "Projects");
    const globalDir = path.join(os.homedir(), ".claude-daemon", "global-graph");
    const results = [];
    // 글로벌 그래프
    if (fs.existsSync(path.join(globalDir, "graph.json"))) {
      try {
        const g = JSON.parse(fs.readFileSync(path.join(globalDir, "graph.json"), "utf-8"));
        const stat = fs.statSync(path.join(globalDir, "graph.json"));
        results.push({ id: "_global", name: "글로벌 (전체 프로젝트)", dir: globalDir, nodes: g.nodes ? g.nodes.length : 0, edges: (g.links || g.edges || []).length, updated: stat.mtime.toISOString() });
      } catch (e) {}
    }
    // 프로젝트별
    if (fs.existsSync(projectsDir)) {
      for (const entry of fs.readdirSync(projectsDir, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        const graphJson = path.join(projectsDir, entry.name, "graphify-out", "graph.json");
        if (!fs.existsSync(graphJson)) continue;
        try {
          const g = JSON.parse(fs.readFileSync(graphJson, "utf-8"));
          const stat = fs.statSync(graphJson);
          results.push({ id: entry.name, name: entry.name, dir: path.join(projectsDir, entry.name), nodes: g.nodes ? g.nodes.length : 0, edges: (g.links || g.edges || []).length, updated: stat.mtime.toISOString() });
        } catch (e) {}
      }
    }
    json({ graphs: results });
  }
  // Skills API: /api/skills — 설치된 스킬 목록
  else if (pathname === "/api/skills" && req.method === "GET") {
    const skillsDir = path.join(os.homedir(), ".claude", "skills");
    if (!fs.existsSync(skillsDir)) return json({ skills: [] });
    try {
      const entries = fs.readdirSync(skillsDir, { withFileTypes: true });
      const skills = [];
      for (const entry of entries) {
        if (!entry.isDirectory()) continue;
        const skillMd = path.join(skillsDir, entry.name, "SKILL.md");
        if (!fs.existsSync(skillMd)) continue;
        const raw = fs.readFileSync(skillMd, "utf-8");
        // frontmatter 파싱
        const fmMatch = raw.match(/^---\n([\s\S]*?)\n---/);
        const skill = { id: entry.name, name: entry.name, description: "", category: "", tags: "", version: "", author: "" };
        if (fmMatch) {
          const fm = fmMatch[1];
          const nameM = fm.match(/^name:\s*(.+)$/m);
          const descM = fm.match(/^description:\s*"?([^"\n]+)"?$/m);
          const catM = fm.match(/category:\s*"?([^"\n,}]+)"?/);
          const tagsM = fm.match(/tags:\s*"?([^"\n}]+)"?/);
          const verM = fm.match(/version:\s*"?([^"\n,}]+)"?/);
          const authM = fm.match(/author:\s*"?([^"\n,}]+)"?/);
          if (nameM) skill.name = nameM[1].trim();
          if (descM) skill.description = descM[1].trim();
          if (catM) skill.category = catM[1].trim();
          if (tagsM) skill.tags = tagsM[1].trim();
          if (verM) skill.version = verM[1].trim();
          if (authM) skill.author = authM[1].trim();
        }
        skills.push(skill);
      }
      skills.sort((a, b) => a.name.localeCompare(b.name));
      json({ skills });
    } catch (e) { json({ error: e.message }, 500); }
  }
  // Skills API: /api/skills/read — 스킬 SKILL.md 전체 내용
  else if (pathname === "/api/skills/read" && req.method === "GET") {
    const id = url.searchParams.get("id");
    if (!id) return json({ error: "id 필요" }, 400);
    const skillMd = path.join(os.homedir(), ".claude", "skills", id, "SKILL.md");
    if (!skillMd.startsWith(path.join(os.homedir(), ".claude", "skills"))) return json({ error: "접근 불가" }, 403);
    if (!fs.existsSync(skillMd)) return json({ error: "스킬 없음" }, 404);
    try {
      const content = fs.readFileSync(skillMd, "utf-8");
      // frontmatter 제거 후 본문만
      const body = content.replace(/^---\n[\s\S]*?\n---\n*/, "");
      json({ id, content: body });
    } catch (e) { json({ error: e.message }, 500); }
  }
  // Skills API: /api/skills/install — 스킬 설치 (SKILL.md 내용을 받아 로컬에 저장)
  else if (pathname === "/api/skills/install" && req.method === "POST") {
    readBody().then(body => {
      const { id, content } = body;
      if (!id || !content) return json({ error: "id, content 필요" }, 400);
      if (/[\/\\]/.test(id)) return json({ error: "잘못된 id" }, 400);
      const skillDir = path.join(os.homedir(), ".claude", "skills", id);
      try {
        fs.mkdirSync(skillDir, { recursive: true });
        fs.writeFileSync(path.join(skillDir, "SKILL.md"), content, "utf-8");
        json({ ok: true, id });
      } catch (e) { json({ error: "설치 실패: " + e.message }, 500); }
    }).catch(e => json({ error: e.message }, 400));
  }
  // Skills API: /api/skills/uninstall — 스킬 제거
  else if (pathname === "/api/skills/uninstall" && req.method === "POST") {
    readBody().then(body => {
      const { id } = body;
      if (!id) return json({ error: "id 필요" }, 400);
      if (/[\/\\]/.test(id)) return json({ error: "잘못된 id" }, 400);
      const skillDir = path.join(os.homedir(), ".claude", "skills", id);
      if (!fs.existsSync(skillDir)) return json({ ok: true, id });
      try {
        fs.rmSync(skillDir, { recursive: true, force: true });
        json({ ok: true, id });
      } catch (e) { json({ error: "제거 실패: " + e.message }, 500); }
    }).catch(e => json({ error: e.message }, 400));
  }
  // Encryption: /api/encryption-key — 브라우저에 암호화 키 전달 (localhost only)
  else if (pathname === "/api/encryption-key" && req.method === "GET") {
    const keyPath = path.join(os.homedir(), ".claude-daemon", "encryption.key");
    if (!fs.existsSync(keyPath)) {
      return json({ error: "No encryption key found" }, 404);
    }
    try {
      const key = fs.readFileSync(keyPath, "utf-8").trim();
      json({ key });
    } catch (e) {
      json({ error: "Failed to read key" }, 500);
    }
  }
  // Share: /share — 문서 공유 뷰어 (HTML 렌더링)
  else if (pathname === "/share" && req.method === "GET") {
    const dir = url.searchParams.get("dir");
    const filePath = url.searchParams.get("path");
    const token = url.searchParams.get("token");
    if (!dir || !filePath) { res.writeHead(400); res.end("dir, path 파라미터 필요"); return; }

    const validated = validateDocsDir(dir);
    if (!validated) { res.writeHead(403); res.end("Forbidden"); return; }
    const fullPath = path.resolve(validated, filePath);
    if (!fullPath.startsWith(validated) || !fs.existsSync(fullPath)) { res.writeHead(404); res.end("Not found"); return; }

    const ext = path.extname(fullPath).toLowerCase();
    const title = path.basename(filePath, ext);
    const isMarkdown = [".md", ".mdx"].includes(ext);
    const isCode = [".js",".ts",".jsx",".tsx",".py",".rb",".go",".rs",".java",".c",".cpp",".h",".sh",".sql",".json",".yaml",".yml",".toml",".css",".html",".xml"].includes(ext);
    const isImage = [".png",".jpg",".jpeg",".gif",".webp",".svg",".bmp"].includes(ext);
    const isText = [".txt",".csv",".log"].includes(ext);

    // 이미지: 직접 서빙
    if (isImage) {
      serveDocsFile(res, dir, filePath);
      return;
    }

    // 텍스트 계열: HTML 뷰어로 렌더링
    let content = "";
    try { content = fs.readFileSync(fullPath, "utf-8"); } catch { res.writeHead(500); res.end("Read error"); return; }

    // 마크다운 내 상대 이미지 경로를 포탈 URL로 변환
    const docDir = filePath.includes("/") ? filePath.split("/").slice(0, -1).join("/") : "";
    if (isMarkdown) {
      content = content.replace(/!\[([^\]]*)\]\((?!http|data:)([^)]+)\)/g, (match, alt, imgPath) => {
        const cleanPath = imgPath.startsWith("./") ? imgPath.slice(2) : imgPath;
        const resolvedImg = docDir ? `${docDir}/${cleanPath}` : cleanPath;
        return `![${alt}](/api/docs/file?dir=${encodeURIComponent(dir)}&path=${encodeURIComponent(resolvedImg)}${token ? "&token=" + token : ""})`;
      });
      // 상대 .md 링크를 공유 뷰어 URL로 변환 (안 하면 포탈이 인증 필요 응답)
      content = content.replace(/(?<!!)\[([^\]]*)\]\((?!https?:|data:|mailto:|#|\/)([^)#]+\.md)(#[^)]*)?\)/g, (match, text, linkPath, hash) => {
        const cleanPath = linkPath.startsWith("./") ? linkPath.slice(2) : linkPath;
        const resolvedDoc = docDir ? `${docDir}/${cleanPath}` : cleanPath;
        return `[${text}](/share?dir=${encodeURIComponent(dir)}&path=${encodeURIComponent(resolvedDoc)}${token ? "&token=" + token : ""}${hash || ""})`;
      });
    }

    const escapedContent = content.replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/&/g, "&amp;");
    const lang = isCode ? ext.slice(1) : "text";

    const html = `<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${title} — PeterVoice</title>
${isMarkdown ? `
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"><\/script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"><\/script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5/github-markdown-light.min.css">
` : `
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/prismjs@1/themes/prism.min.css">
<script src="https://cdn.jsdelivr.net/npm/prismjs@1/prism.min.js"><\/script>
<script src="https://cdn.jsdelivr.net/npm/prismjs@1/plugins/autoloader/prism-autoloader.min.js"><\/script>
`}
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #fff; color: #24292f; }
  .header { background: #f6f8fa; border-bottom: 1px solid #d0d7de; padding: 12px 24px; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 16px; font-weight: 600; }
  .header .badge { font-size: 11px; color: #656d76; background: #eaeef2; padding: 2px 8px; border-radius: 12px; }
  .container { max-width: 900px; margin: 0 auto; padding: 32px 24px; }
  .markdown-body { font-size: 15px; line-height: 1.7; }
  .markdown-body img { max-width: 100%; border-radius: 8px; margin: 16px 0; cursor: pointer; }
  .code-wrap { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 8px; overflow-x: auto; padding: 16px; font-size: 13px; line-height: 1.5; }
  .code-wrap code { white-space: pre; }
  .lightbox { display: none; position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,.9); align-items: center; justify-content: center; cursor: pointer; }
  .lightbox.active { display: flex; }
  .lightbox img { max-width: 95vw; max-height: 95vh; object-fit: contain; }
</style>
</head>
<body>
<div class="header">
  <h1>${title}</h1>
  <span class="badge">${ext.slice(1).toUpperCase()}</span>
</div>
<div class="container">
${isMarkdown
  ? `<div id="md" class="markdown-body"></div>
<script>
const raw = ${JSON.stringify(content)};
document.getElementById('md').innerHTML = marked.parse(raw);
// Mermaid 다이어그램 렌더링
mermaid.initialize({ startOnLoad: false, theme: 'default' });
document.querySelectorAll('code.language-mermaid').forEach((el, i) => {
  const pre = el.parentElement;
  const div = document.createElement('div');
  div.className = 'mermaid';
  div.textContent = el.textContent;
  pre.replaceWith(div);
});
mermaid.run();
// 이미지 라이트박스
document.addEventListener('click', e => {
  if (e.target.tagName === 'IMG' && e.target.closest('.markdown-body')) {
    const lb = document.getElementById('lightbox');
    lb.querySelector('img').src = e.target.src;
    lb.classList.add('active');
  }
});
<\/script>`
  : `<div class="code-wrap"><code class="language-${lang}">${escapedContent}</code></div>
<script>Prism.highlightAll();<\/script>`
}
</div>
<div id="lightbox" class="lightbox" onclick="this.classList.remove('active')"><img src="" alt=""></div>
</body>
</html>`;

    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(html);
  }
  // Git API: /api/git/repos — 디렉토리에서 git 리포 스캔
  else if (pathname === "/api/git/repos" && req.method === "GET") {
    const dir = url.searchParams.get("dir");
    if (!dir) return json({ error: "dir 파라미터 필요" }, 400);
    json(apiGitRepos(dir));
  }
  // Git API: /api/git/branches — 리포의 브랜치 목록
  else if (pathname === "/api/git/branches" && req.method === "GET") {
    const dir = url.searchParams.get("dir");
    if (!dir) return json({ error: "dir 파라미터 필요" }, 400);
    json(apiGitBranches(dir));
  }
  // Git API: /api/git/commits — 커밋 목록
  else if (pathname === "/api/git/commits" && req.method === "GET") {
    const dir = url.searchParams.get("dir");
    const branch = url.searchParams.get("branch") || "HEAD";
    const limit = parseInt(url.searchParams.get("limit") || "30");
    if (!dir) return json({ error: "dir 파라미터 필요" }, 400);
    json(apiGitCommits(dir, branch, limit));
  }
  // Git API: /api/git/diff — 커밋의 diff 데이터
  else if (pathname === "/api/git/diff" && req.method === "GET") {
    const dir = url.searchParams.get("dir");
    const commit = url.searchParams.get("commit");
    if (!dir || !commit) return json({ error: "dir, commit 파라미터 필요" }, 400);
    json(apiGitDiff(dir, commit));
  }
  // Git API: /api/git/diff-range — 커밋 범위의 diff 데이터
  else if (pathname === "/api/git/diff-range" && req.method === "GET") {
    const dir = url.searchParams.get("dir");
    const from = url.searchParams.get("from");
    const to = url.searchParams.get("to");
    if (!dir || !from || !to) return json({ error: "dir, from, to 파라미터 필요" }, 400);
    json(apiGitDiffRange(dir, from, to));
  }
  else if (pathname === "/api/publish" && req.method === "POST") {
    readBody().then(body => json(execPublish(body)));
  }
  else if (pathname === "/api/rebuild" && req.method === "POST") {
    readBody().then(body => json(execRebuild(body)));
  }
  else if (pathname === "/api/unpublish" && req.method === "POST") {
    readBody().then(body => json(execUnpublish(body)));
  }
  else if (pathname === "/api/browser/frame" && req.method === "GET") {
    // 브라우저 인계: 현재 화면 프레임 (JPEG base64) + 뷰포트/URL
    (async () => {
      const port = cdpPortForDir(url.searchParams.get("dir"));
      if (!port) return json({ error: "잘못된 dir" }, 403);
      let s;
      try {
        s = await cdpConnect(port, url.searchParams.get("url") || "");
      } catch (e) {
        // CDP 죽음 → 셀프힐: 인계 뷰어가 900ms~3s 로 폴링 중이므로 여기서 브라우저를
        // 재기동해 두면 몇 초 안에 자동 복구된다 (2026-08-18 저녁 chromium 사망으로
        // 유저가 '연결 중' 무한 대기한 실사고 — infohub 제보)
        const starting = maybeStartBrowser(port);
        return json({ error: "browser_unavailable", starting, detail: String((e && e.message) || e) }, 502);
      }
      // 인계 종료 시 뷰포트 오버라이드 원복 — 에이전트가 세션을 이어받을 때
      // 폰 크기 뷰포트가 남아 스크래핑이 모바일 레이아웃을 보게 되는 것을 방지
      if (url.searchParams.get("reset") === "1") {
        try { await cdpSend(s, "Emulation.clearDeviceMetricsOverride", {}); } catch {}
        s.appliedViewport = "";
        return json({ ok: true });
      }
      // 유저 창 크기에 원격 뷰포트를 맞춘다 (2026-08-19 개선):
      // 폰으로 열면 원격 브라우저가 폰 크기(모바일 레이아웃)가 되어 화면을 꽉 채우고,
      // dpr(최대 2)로 캡처 해상도를 올려 클라이언트 확대 시에도 글자가 선명하다.
      // 파라미터가 없으면(구버전 웹) 기존 동작 그대로.
      const vw = Math.min(2560, parseInt(url.searchParams.get("vw")) || 0);
      const vh = Math.min(2560, parseInt(url.searchParams.get("vh")) || 0);
      const dpr = Math.max(1, Math.min(2, parseInt(url.searchParams.get("dpr")) || 1));
      if (vw >= 320 && vh >= 320) {
        const want = vw + "x" + vh + "@" + dpr;
        if (s.appliedViewport !== want) {
          await cdpSend(s, "Emulation.setDeviceMetricsOverride", {
            width: vw, height: vh, deviceScaleFactor: dpr, mobile: vw < 768,
          });
          s.appliedViewport = want;
        }
      }
      const [shot, metrics] = await Promise.all([
        cdpSend(s, "Page.captureScreenshot", { format: "jpeg", quality: 60 }),
        cdpSend(s, "Page.getLayoutMetrics", {}),
      ]);
      let pageInfo = {};
      try {
        const t = await cdpHttpJson(port, "/json/list");
        const p = (t || []).find(x => x.id === s.targetId) ||
          (t || []).find(x => x.type === "page" && !/^devtools:/.test(x.url || ""));
        pageInfo = { url: p && p.url, title: p && p.title };
      } catch {}
      const vp = metrics.cssLayoutViewport || metrics.layoutViewport || {};
      json({
        ok: true,
        image: shot.data,
        viewport: { w: vp.clientWidth || 0, h: vp.clientHeight || 0 },
        url: pageInfo.url || "",
        title: pageInfo.title || "",
      });
    })().catch((e) => json({ error: "browser_unavailable", detail: String((e && e.message) || e) }, 502));
  }
  else if (pathname === "/api/browser/input" && req.method === "POST") {
    // 브라우저 인계: 입력 전달. 이벤트 내용은 절대 로깅하지 않는다 (자격증명 위생).
    readBody().then(async (body) => {
      const port = cdpPortForDir(body && body.dir);
      if (!port) return json({ error: "잘못된 dir" }, 403);
      const s = await cdpConnect(port, (body && body.url) || "");
      const events = Array.isArray(body.events) ? body.events.slice(0, 20) : [];
      for (const ev of events) await cdpDispatchEvent(s, ev);
      json({ ok: true });
    }).catch((e) => json({ error: "browser_unavailable", detail: String((e && e.message) || e) }, 502));
  }
  else {
    json({ error: "Not found" }, 404);
  }
});

// ─── Terminal WebSocket Server ───────────────────────

// tmux 경로 — launchd 환경은 PATH가 제한적이므로 절대경로 우선
const TMUX_CMD = ["/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"]
  .find(p => { try { return require("fs").existsSync(p); } catch { return false; } }) || "tmux";

// pty 세션 풀: key → { pty, clients: Set<ws> }
const ptySessions = {};

function getSessionKey(project, branch) {
  const sanitize = (s) => s.replace(/[:.]/g, "_");
  return branch ? `${sanitize(project)}__br${branch}` : sanitize(project);
}

function getProjectDirectory(projectId) {
  const config = loadConfig();
  // config의 project_dirs 확인
  const projectDirs = config.project_dirs || {};
  if (projectDirs[projectId]) return projectDirs[projectId];
  // ~/Projects/{projectId} fallback
  for (const base of PROJECTS_DIRS) {
    const dir = path.join(base, projectId);
    if (fs.existsSync(dir)) return dir;
  }
  return os.homedir();
}

const PORTAL_ENV = {
  ...process.env,
  TERM: "xterm-256color",
  LANG: "en_US.UTF-8",
  PATH: `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${process.env.PATH || ""}`,
};

function tmuxHasSession(sessionKey) {
  const check = spawnSync(TMUX_CMD, ["has-session", "-t", sessionKey], { env: PORTAL_ENV });
  return check.status === 0;
}

function ensureTmuxSession(sessionKey, projectDir, mode = "claude", promptFile = null, sessionId = null, resume = false) {
  // tmux 세션 존재 여부 확인 (spawnSync으로 직접 실행)
  if (tmuxHasSession(sessionKey)) {
    // 기존 세션도 마우스 스크롤 가능하도록 보장
    spawnSync(TMUX_CMD, ["set-option", "-t", sessionKey, "mouse", "on"], { env: PORTAL_ENV });
    return true;
  }

  // 세션 시작 명령 결정
  let startCmd;
  if (mode === "shell") {
    // 범용 셸 모드: 맨 zsh/로그인 셸 (claude가 불능일 때의 비상 통로)
    startCmd = [process.env.SHELL || "/bin/zsh", "-l"];
  } else {
    // 기본: 프로젝트에 연결된 claude 대화형 세션
    // 채팅 모드와 동일하게 조합된 프로젝트/브랜치 프롬프트를 시스템 프롬프트로 주입
    const claudeCmd = ["/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
      .find(p => fs.existsSync(p)) || "claude";
    startCmd = [claudeCmd, "--dangerously-skip-permissions"];
    // 이전 터미널 대화 이어가기: 트랜스크립트가 있으면 --resume, 없으면 새 --session-id
    if (sessionId && resume) {
      startCmd.push("--resume", sessionId);
    } else if (sessionId) {
      startCmd.push("--session-id", sessionId);
    }
    if (promptFile && fs.existsSync(promptFile)) {
      startCmd.push("--append-system-prompt-file", promptFile);
    }
  }

  // 스크롤백: history-limit은 pane 생성 전에 설정해야 적용됨 → 글로벌 옵션으로 먼저 세팅
  spawnSync(TMUX_CMD, ["set-option", "-g", "history-limit", "50000"], { env: PORTAL_ENV });

  const result = spawnSync(
    TMUX_CMD,
    ["new-session", "-d", "-s", sessionKey, "-c", projectDir, ...startCmd],
    { env: PORTAL_ENV }
  );
  if (result.status !== 0) {
    throw new Error(result.stderr?.toString() || "tmux new-session failed");
  }

  // 마우스/터치 휠로 이전 출력을 스크롤(copy-mode 진입)할 수 있게 마우스 모드 ON
  spawnSync(TMUX_CMD, ["set-option", "-t", sessionKey, "mouse", "on"], { env: PORTAL_ENV });

  return false;
}

if (ptyModule && WebSocketServer) {
  const wss = new WebSocketServer({ server, path: "/terminal" });

  wss.on("connection", (ws, req) => {
    const url = new URL(req.url, `http://localhost:${PORT}`);
    const project = url.searchParams.get("project") || "general";
    const branch = url.searchParams.get("branch") || null;
    const mode = url.searchParams.get("mode") === "shell" ? "shell" : "claude";
    // 인증: X-Api-Key 헤더(서버 간) 또는 단기 JWT(?token=).
    // 브라우저 WebSocket 은 헤더를 못 붙이므로 5분 TTL 서명 토큰을 쿼리로 받는다.
    // raw 키를 URL 쿼리(?key=)로 받는 방식은 로그 잔존 위험으로 제거 (2026-08-06 보안 점검).
    const config = loadConfig();
    const headerKey = req.headers["x-api-key"];
    const wsToken = url.searchParams.get("token");
    const wsAuthed = (headerKey && headerKey === config.api_key) ||
      (wsToken && verifyJwt(wsToken, config.api_key));
    if (!wsAuthed) {
      ws.send(JSON.stringify({ type: "error", message: "Unauthorized" }));
      ws.close();
      return;
    }

    // 셸 모드: 데몬 설정으로 차단 가능 (기본 허용). claude가 불능일 때의 비상 통로.
    if (mode === "shell" && config.allow_shell_mode === false) {
      ws.send(JSON.stringify({ type: "error", message: "Shell mode disabled" }));
      ws.close();
      return;
    }

    // 셸 모드는 프로젝트와 무관한 단일 글로벌 세션(홈 디렉토리)
    const sessionKey = mode === "shell" ? "__shell__" : getSessionKey(project, branch);

    // 새 세션을 만들 때만 prepare 호출(디렉토리/프롬프트/세션ID 해석).
    // 기존 세션이 살아있으면 그대로 attach — 세션ID 레지스트리 드리프트 방지.
    let projectDir = mode === "shell" ? os.homedir() : getProjectDirectory(project);
    let promptFile = null, sessionId = null, resume = false;
    const sessionExists = tmuxHasSession(sessionKey);
    if (mode !== "shell" && !sessionExists) {
      try {
        const prepArgs = ["terminal_prepare.py", "--project", project];
        if (branch) prepArgs.push("--branch", branch);
        const prep = spawnSync("python3", prepArgs, {
          cwd: __dirname, env: PORTAL_ENV, timeout: 30000,
        });
        if (prep.status === 0 && prep.stdout) {
          const out = JSON.parse(prep.stdout.toString().trim().split("\n").pop());
          if (out.dir) projectDir = out.dir;
          if (out.prompt_file) promptFile = out.prompt_file;
          if (out.session_id) { sessionId = out.session_id; resume = !!out.resume; }
        } else {
          console.warn("[terminal] terminal_prepare failed:", (prep.stderr || "").toString().slice(0, 300));
        }
      } catch (e) {
        console.warn("[terminal] terminal_prepare error (degraded):", e.message);
      }
    }

    console.log(`[terminal] WS connect: mode=${mode}, session=${sessionKey}, dir=${projectDir}, ${sessionExists ? "attach" : (resume ? "resume" : "new")}`);

    // tmux 세션 준비
    try {
      ensureTmuxSession(sessionKey, projectDir, mode, promptFile, sessionId, resume);
    } catch (e) {
      console.error("[terminal] tmux session create failed:", e.message);
      ws.send(`\r\n[Error] Failed to create tmux session: ${e.message}\r\n`);
      ws.close();
      return;
    }

    // node-pty로 tmux attach
    const ptyProcess = ptyModule.spawn(TMUX_CMD, ["attach-session", "-t", sessionKey], {
      name: "xterm-256color",
      cols: 220,
      rows: 50,
      cwd: projectDir,
      env: { ...process.env, TERM: "xterm-256color", LANG: "en_US.UTF-8" },
    });

    // pty 출력 → WebSocket
    ptyProcess.onData((data) => {
      if (ws.readyState === ws.OPEN) {
        ws.send(data);
      }
    });

    ptyProcess.onExit(() => {
      console.log(`[terminal] pty exited: ${sessionKey}`);
      if (ws.readyState === ws.OPEN) ws.close();
    });

    // WebSocket 입력 → pty
    ws.on("message", (msg) => {
      try {
        const parsed = JSON.parse(msg);
        if (parsed.type === "resize") {
          ptyProcess.resize(parsed.cols, parsed.rows);
        } else if (parsed.type === "input") {
          ptyProcess.write(parsed.data);
        }
      } catch {
        // raw input (string)
        ptyProcess.write(msg.toString());
      }
    });

    ws.on("close", () => {
      console.log(`[terminal] WS close: ${sessionKey}`);
      try { ptyProcess.kill(); } catch {}
    });

    ws.on("error", (e) => {
      console.error(`[terminal] WS error: ${e.message}`);
      try { ptyProcess.kill(); } catch {}
    });
  });

  console.log("[terminal] WebSocket server ready at /terminal");

  // ── Idle reaper ─────────────────────────────────────────────
  // 미접속(attached==0) 상태로 idle 임계(기본 360분)를 넘긴 터미널 세션을 정리.
  // claude 트랜스크립트는 디스크에 남으므로, 재접속 시 --resume으로 대화 이어감.
  // config.terminal_idle_minutes = 0 이면 비활성화.
  function reapIdleTerminalSessions() {
    const idleMin = loadConfig().terminal_idle_minutes;
    const limit = (idleMin === undefined || idleMin === null) ? 360 : Number(idleMin);
    if (!limit || limit <= 0) return;
    const r = spawnSync(TMUX_CMD, ["list-sessions", "-F", "#{session_name}|#{session_activity}|#{session_attached}"], { env: PORTAL_ENV });
    if (r.status !== 0 || !r.stdout) return;
    const nowSec = Math.floor(Date.now() / 1000);
    for (const line of r.stdout.toString().trim().split("\n")) {
      if (!line) continue;
      const [name, act, attached] = line.split("|");
      if (Number(attached) >= 1) continue;        // 보고 있는 사람 있음 → 보호
      const idleSec = nowSec - Number(act);
      if (idleSec < 60) continue;                 // 레이스 가드(방금 활동)
      if (idleSec > limit * 60) {
        spawnSync(TMUX_CMD, ["kill-session", "-t", name], { env: PORTAL_ENV });
        console.log(`[terminal] reaped idle session: ${name} (idle ${Math.floor(idleSec / 60)}m, limit ${limit}m)`);
      }
    }
  }
  setInterval(reapIdleTerminalSessions, 5 * 60 * 1000);
} else {
  console.warn("[terminal] WebSocket server disabled (node-pty/ws not available)");
}

server.listen(PORT, () => {
  console.log(`PeterVoice Home Portal running on http://localhost:${PORT}`);
});
