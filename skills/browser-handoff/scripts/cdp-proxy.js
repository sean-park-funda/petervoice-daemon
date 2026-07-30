// CDP TCP 브리지: 컨테이너 eth0:9222 → 127.0.0.1:9222
//
// 최신 크롬(151+)은 보안상 CDP 를 127.0.0.1 에만 바인딩한다
// (--remote-debugging-address=0.0.0.0 무시). 포트 퍼블리시의 DNAT 목적지는
// 컨테이너 eth0 IP 라서 그대로는 호스트(pv-portal)가 접근할 수 없다.
// node 표준 라이브러리만 쓰는 이 브리지가 eth0:9222 를 열어 루프백으로 넘긴다.
// (127.0.0.1:9222 는 크롬이 점유 중이므로 eth0 IP 에 명시 바인딩 — 충돌 없음)
//
// 컨테이너 전용 — start-browser.sh 가 $HOME=/home/agent 일 때만 띄운다.
const net = require("net");
const os = require("os");

const addrs = Object.values(os.networkInterfaces())
  .flat()
  .filter((a) => a && a.family === "IPv4" && !a.internal);
if (!addrs.length) {
  console.error("no non-loopback IPv4 interface");
  process.exit(1);
}
const ip = addrs[0].address;

const server = net.createServer((client) => {
  const upstream = net.connect(9222, "127.0.0.1");
  client.pipe(upstream);
  upstream.pipe(client);
  client.on("error", () => upstream.destroy());
  upstream.on("error", () => client.destroy());
});
server.on("error", (e) => {
  console.error("bind failed:", e.message);
  process.exit(1);
});
server.listen(9222, ip, () => console.log(`cdp-proxy: ${ip}:9222 -> 127.0.0.1:9222`));
