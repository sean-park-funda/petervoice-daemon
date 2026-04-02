#!/usr/bin/env node
/**
 * PeterVoice Home Portal — 유저 맥미니 대시보드
 * 경량 웹서버 (순수 Node.js, 외부 의존성 없음)
 *
 * Usage: node home-portal.js [--port 3000] [--config-dir ~/.claude-daemon]
 */

const http = require("http");
const fs = require("fs");
const path = require("path");
const os = require("os");
const { execSync } = require("child_process");

// ─── Config ──────────────────────────────────────────
const args = process.argv.slice(2);
const PORT = parseInt(getArg("--port") || "3000");
const CONFIG_DIR = getArg("--config-dir") || path.join(os.homedir(), ".claude-daemon");
const SITES_FILE = path.join(os.homedir(), ".petervoice-sites", "sites.json");
const PROJECTS_DIR = path.join(os.homedir(), "Projects");

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
    const entries = fs.readdirSync(PROJECTS_DIR, { withFileTypes: true });
    return entries
      .filter(e => e.isDirectory() && !e.name.startsWith("."))
      .map(e => {
        const dir = path.join(PROJECTS_DIR, e.name);
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

        return {
          name: e.name,
          dir,
          framework,
          published,
          url: siteEntry ? siteEntry[1].url : null,
          port: siteEntry ? siteEntry[1].port : null,
        };
      });
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
  // 보안: ~/Projects/ 하위만 허용
  const baseDir = path.resolve(PROJECTS_DIR);
  const targetDir = path.resolve(baseDir, relDir || "");

  if (!targetDir.startsWith(baseDir)) {
    return { error: "접근 불가 경로" };
  }

  if (!fs.existsSync(targetDir) || !fs.statSync(targetDir).isDirectory()) {
    return { error: "디렉토리 없음" };
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

function renderHTML() {
  const config = loadConfig();
  const username = (config.bot_name || "user").toLowerCase().replace(/\s/g, "-");

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
  const username = (config.bot_name || "user").toLowerCase().replace(/\s/g, "-");
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
  const username = (config.bot_name || "user").toLowerCase().replace(/\s/g, "-");
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

function getProjectDirs() {
  const config = loadConfig();
  return config.project_dirs || {};
}

function apiDocsList(projectId) {
  const dirs = getProjectDirs();
  const projectDir = dirs[projectId];
  if (!projectDir) return { error: `프로젝트 없음: ${projectId}` };

  const docsDir = path.join(projectDir, "docs");
  if (!fs.existsSync(docsDir)) return { files: [] };

  const files = [];
  function scan(dir, prefix) {
    try {
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      for (const entry of entries) {
        if (entry.name.startsWith(".")) continue;
        const fullPath = path.join(dir, entry.name);
        const relPath = prefix ? `${prefix}/${entry.name}` : entry.name;
        if (entry.isDirectory()) {
          scan(fullPath, relPath);
        } else if (entry.name.endsWith(".md")) {
          const stat = fs.statSync(fullPath);
          files.push({
            path: relPath,
            title: entry.name.replace(/\.md$/, ""),
            size: stat.size,
            modified: stat.mtime.toISOString(),
          });
        }
      }
    } catch {}
  }
  scan(docsDir, "");
  return { project: projectId, files };
}

function apiDocsRead(projectId, docPath) {
  const dirs = getProjectDirs();
  const projectDir = dirs[projectId];
  if (!projectDir) return { error: `프로젝트 없음: ${projectId}` };

  const docsDir = path.resolve(projectDir, "docs");
  const filePath = path.resolve(docsDir, docPath);

  // path traversal 방지
  if (!filePath.startsWith(docsDir)) return { error: "접근 불가 경로" };
  if (!filePath.endsWith(".md")) return { error: ".md 파일만 지원" };
  if (!fs.existsSync(filePath)) return { error: "파일 없음" };

  try {
    const content = fs.readFileSync(filePath, "utf-8");
    const stat = fs.statSync(filePath);
    return {
      path: docPath,
      title: path.basename(docPath, ".md"),
      content,
      size: stat.size,
      modified: stat.mtime.toISOString(),
    };
  } catch {
    return { error: "읽기 실패" };
  }
}

// ─── Auth ────────────────────────────────────────────

function isLocalRequest(req) {
  const addr = req.socket.remoteAddress;
  return addr === "127.0.0.1" || addr === "::1" || addr === "::ffff:127.0.0.1";
}

function verifyAuth(req) {
  // localhost 요청은 인증 불필요
  if (isLocalRequest(req)) return true;

  // 외부 요청은 API key 필요
  const config = loadConfig();
  const apiKey = config.api_key;
  if (!apiKey) return false;

  const header = req.headers["x-api-key"] || req.headers["authorization"];
  if (!header) return false;

  // "Bearer pv_xxx" 또는 "pv_xxx" 형태 모두 지원
  const token = header.startsWith("Bearer ") ? header.slice(7) : header;
  return token === apiKey;
}

// ─── Server ──────────────────────────────────────────

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const pathname = url.pathname;

  // CORS
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Api-Key, Authorization");
  if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }

  // JSON helper
  const json = (data, status = 200) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(data));
  };

  // Auth check for /api/docs/ (외부 요청 시 인증 필수)
  if (pathname.startsWith("/api/docs/") && !verifyAuth(req)) {
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
    res.end(renderHTML());
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
  // Docs API: /api/docs/:project — 문서 목록
  else if (pathname.match(/^\/api\/docs\/[^/]+$/) && req.method === "GET") {
    const projectId = decodeURIComponent(pathname.split("/api/docs/")[1]);
    json(apiDocsList(projectId));
  }
  // Docs API: /api/docs/:project/:path — 문서 내용
  else if (pathname.match(/^\/api\/docs\/[^/]+\/.+/) && req.method === "GET") {
    const rest = pathname.slice("/api/docs/".length);
    const slashIdx = rest.indexOf("/");
    const projectId = decodeURIComponent(rest.slice(0, slashIdx));
    const docPath = decodeURIComponent(rest.slice(slashIdx + 1));
    json(apiDocsRead(projectId, docPath));
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
