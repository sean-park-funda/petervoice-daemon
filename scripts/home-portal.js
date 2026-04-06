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
const { execSync } = require("child_process");

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

function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(path.join(CONFIG_DIR, "config.json"), "utf-8"));
  } catch { return {}; }
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
    : (config.bot_name || "user").toLowerCase().replace(/\s/g, "-");
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
    : (config.bot_name || "user").toLowerCase().replace(/\s/g, "-");
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
  // path traversal 방지: 홈 디렉토리 하위만 허용
  const homeDir = os.homedir();
  const resolved = path.resolve(dir);
  if (!resolved.startsWith(homeDir)) return null;
  return resolved;
}

function apiDocsList(docsDir) {
  const validated = validateDocsDir(docsDir);
  if (!validated) return { error: "접근 불가 경로" };
  if (!fs.existsSync(validated)) return { documents: [] };

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
  if (!name || !/^[a-zA-Z0-9가-힣_\-. ]+$/.test(name)) return { error: "잘못된 폴더명" };

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

// ─── Auth ────────────────────────────────────────────

// 세션 스토어 (메모리 — 재시작 시 초기화, 재인증이면 충분)
const sessions = new Map(); // sessionToken → { createdAt, expiresAt }
const SESSION_MAX_AGE = 86400; // 24시간

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

function verifyAuth(req) {
  // 로컬 요청은 인증 불필요
  if (!isTunnelRequest(req)) return true;

  const config = loadConfig();
  const apiKey = config.api_key;
  if (!apiKey) return false;

  // 1. 세션 쿠키 인증
  const cookies = parseCookies(req);
  const sessionToken = cookies["pv_session"];
  if (sessionToken && sessions.has(sessionToken)) {
    const session = sessions.get(sessionToken);
    if (session.expiresAt > Date.now()) return true;
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
    // JWT 검증
    if (verifyJwt(jwt, apiKey)) return true;
  }

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
  for (const [token, session] of sessions) {
    if (session.expiresAt <= now) sessions.delete(token);
  }
}, 3600000);

// ─── Server ──────────────────────────────────────────

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const pathname = url.pathname;

  // CORS — 특정 origin만 허용 (브라우저 직접 통신)
  const ALLOWED_ORIGINS = ["https://peter-voice.vercel.app", "http://localhost:3001"];
  const origin = req.headers.origin;
  if (origin && ALLOWED_ORIGINS.includes(origin)) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Access-Control-Allow-Credentials", "true");
  } else if (!origin) {
    // origin 없는 요청 (같은 도메인, curl 등) 허용
    res.setHeader("Access-Control-Allow-Origin", "*");
  }
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Api-Key, Authorization");
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

  // Read body helper
  const readBody = () => new Promise((resolve) => {
    let body = "";
    req.on("data", c => body += c);
    req.on("end", () => {
      try { resolve(JSON.parse(body)); } catch { resolve({}); }
    });
  });

  // Routes
  if (pathname === "/" && req.method === "GET") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(renderHTML(req));
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
  // Docs API: /api/docs/upload — 파일 업로드
  else if (pathname === "/api/docs/upload" && req.method === "POST") {
    parseMultipart(req).then(parts => {
      const dir = parts.dir;
      const subpath = parts.path || "";
      const file = parts.file;
      if (!dir || !file) return json({ error: "dir, file 필요" }, 400);
      const validated = validateDocsDir(dir);
      if (!validated) return json({ error: "접근 불가 경로" }, 403);
      const targetDir = subpath ? path.join(validated, path.dirname(subpath)) : validated;
      const fileName = subpath ? path.basename(subpath) : file.filename;
      const targetPath = path.resolve(targetDir, fileName);
      if (!targetPath.startsWith(validated)) return json({ error: "접근 불가 경로" }, 403);
      if (file.data.length > 50 * 1024 * 1024) return json({ error: "50MB 초과" }, 413);
      try {
        fs.mkdirSync(targetDir, { recursive: true });
        fs.writeFileSync(targetPath, file.data);
        json({ ok: true, path: path.relative(validated, targetPath), size: file.data.length });
      } catch (e) {
        json({ error: "저장 실패: " + e.message }, 500);
      }
    }).catch(e => json({ error: "업로드 파싱 실패: " + e.message }, 400));
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
      if (!dir || !filePath) return json({ error: "dir, filePath 필요" }, 400);
      const base = validateDocsDir(dir);
      if (!base) return json({ error: "접근 불가 경로" }, 403);
      const full = path.resolve(base, filePath);
      if (!full.startsWith(base)) return json({ error: "접근 불가 경로" }, 403);
      if (!fs.existsSync(full)) return json({ error: "파일 없음" }, 404);
      try {
        const stat = fs.statSync(full);
        if (stat.isDirectory()) {
          fs.rmSync(full, { recursive: true, force: true });
        } else {
          fs.unlinkSync(full);
        }
        json({ ok: true });
      } catch (e) { json({ error: "삭제 실패: " + e.message }, 500); }
    }).catch(e => json({ error: e.message }, 400));
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
    const id = params.get("id");
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
  else if (pathname === "/api/publish" && req.method === "POST") {
    readBody().then(body => json(execPublish(body)));
  }
  else if (pathname === "/api/rebuild" && req.method === "POST") {
    readBody().then(body => json(execRebuild(body)));
  }
  else if (pathname === "/api/unpublish" && req.method === "POST") {
    readBody().then(body => json(execUnpublish(body)));
  }
  else {
    json({ error: "Not found" }, 404);
  }
});

server.listen(PORT, () => {
  console.log(`PeterVoice Home Portal running on http://localhost:${PORT}`);
});
