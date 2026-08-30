#!/usr/bin/env python3
"""route_scan.py — 경로 위 장소 스캔 (근거 기반, 2026-08-30 Sean 지시로 재정비)

원칙
 1. 경로·소요시간·이탈시간은 전부 네이버 Directions(실시간)로 계산한다. OSRM/추정 금지.
 2. 장소는 경로 좌표를 --step km 간격으로 샘플링해 각 지점 좌표로 네이버 플레이스 검색한 뒤
    경로에서 --max-off km 이내만 남긴다. 도시명 키워드 검색으로 "없다"를 말하지 않는다.
 3. 후보마다 네이버 경유 재계산으로 도착시각·이탈분·이후 잔여시간을 구하고, 매장 페이지에서
    영업시간·메뉴가격을 뽑는다.
 4. 출력 첫 줄에 method 를 남겨 답변에 근거를 그대로 적을 수 있게 한다.

사용
  python3 route_scan.py --start "lng,lat" --goal "lng,lat" --query 맥도날드 [--depart 20:30]
        [--force-via "lng,lat"]  # 특정 고속도로를 타게 하려면 그 도로 위 좌표 (route_with_via.py 로 뽑거나 이전 경로 path 에서)
        [--step 12] [--max-off 1.0] [--window 60-180]  # 출발 후 N~M분 구간만
        [--map out.json --title "..."]  # 지도 상태 파일 생성 (segments+eta 핀) → /api/files/upload 로 올려 [[map/show:URL]]
"""
import argparse, json, math, os, re, sys, urllib.parse, urllib.request, datetime, concurrent.futures as cf

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1", "Accept-Language": "ko"}


def http(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def naver_headers():
    i, s = os.environ.get("NAVER_MAP_CLIENT_ID"), os.environ.get("NAVER_MAP_CLIENT_SECRET")
    if not i or not s:
        sys.exit("NAVER_MAP_CLIENT_ID / NAVER_MAP_CLIENT_SECRET 환경변수 필요")
    return {"x-ncp-apigw-api-key-id": i, "x-ncp-apigw-api-key": s}


def naver_route(start, goal, waypoints=None):
    q = {"start": start, "goal": goal, "option": "trafast"}
    if waypoints:
        q["waypoints"] = waypoints
    d = json.loads(http("https://maps.apigw.ntruss.com/map-direction/v1/driving?" + urllib.parse.urlencode(q), naver_headers()))
    if "route" not in d:
        sys.exit(f"네이버 경로 실패: {d}")
    return d["route"]["trafast"][0]


def km(a, b):  # (lng,lat)
    la1, lo1, la2, lo2 = map(math.radians, (a[1], a[0], b[1], b[0]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def cum_km(path):
    c = [0.0]
    for i in range(1, len(path)):
        c.append(c[-1] + km(path[i - 1], path[i]))
    return c


def geo_search(query, pt):
    url = f"https://m.place.naver.com/place/list?query={urllib.parse.quote(query)}&x={pt[0]}&y={pt[1]}"
    try:
        t = http(url, UA)
    except Exception:
        return []
    i = t.find("__APOLLO_STATE__")
    h = t[i:i + 600000]
    key = query[:2]
    return re.findall(r'"id":"(\d+)","name":"([^"]*' + re.escape(key) + r'[^"]*)".{0,800}?"x":"(12[0-9.]+)","y":"(3[0-9.]+)"', h)


def place_info(pid):
    out = {"hours": None, "menu": [], "addr": None, "rating": None, "reviews": None}
    for kind in ("restaurant", "place"):
        try:
            t = http(f"https://m.place.naver.com/{kind}/{pid}/home", UA)
        except Exception:
            continue
        i = t.find("__APOLLO_STATE__")
        if i < 0:
            continue
        h = t[i:i + 400000]
        hrs = re.findall(r'"day":"([^"]+)","businessHours":\{[^}]*"start":"([^"]*)","end":"([^"]*)"', h)
        if hrs:
            out["hours"] = f"{hrs[0][0]} {hrs[0][1]}~{hrs[0][2]}"
        br = re.findall(r'"breakHours":\[\{[^}]*"start":"([^"]*)","end":"([^"]*)"', h)
        if br:
            out["hours"] += f" (브레이크 {br[0][0]}~{br[0][1]})"
        a = re.findall(r'"roadAddress":"([^"]+)"', h)
        out["addr"] = a[0] if a else None
        r = re.findall(r'"visitorReviewScore":"?([0-9.]+)', h)
        out["rating"] = r[0] if r else None
        c = re.findall(r'"visitorReviewsTotal":([0-9]+)', h)
        out["reviews"] = int(c[0]) if c else None
        if kind == "restaurant":
            try:
                m = http(f"https://m.place.naver.com/restaurant/{pid}/menu/list", UA)
                s = m[m.find("__APOLLO_STATE__"):][:400000]
                items = re.findall(r'"name":"([^"]{1,40})","price":"([0-9,]+)"', s)
                seen = []
                [seen.append(x) for x in items if x not in seen]
                out["menu"] = [f"{n} {p}" for n, p in seen[:8]]
            except Exception:
                pass
        if out["hours"] or out["addr"]:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="lng,lat")
    ap.add_argument("--goal", required=True, help="lng,lat")
    ap.add_argument("--query", required=True, help="검색어 (맥도날드, 갈비탕, 사우나, 주유소, 휴게소 ...)")
    ap.add_argument("--depart", default=None, help="출발 HH:MM (기본 지금)")
    ap.add_argument("--force-via", default=None, help="경로 강제용 도로 위 좌표 lng,lat (여러 개 | 구분)")
    ap.add_argument("--step", type=float, default=12)
    ap.add_argument("--max-off", type=float, default=1.0)
    ap.add_argument("--window", default=None, help="출발 후 분 범위 예: 60-180")
    ap.add_argument("--top", type=int, default=8, help="이탈 계산할 최대 후보 수(경로 근접순)")
    ap.add_argument("--map", default=None, help="지도 상태 JSON 출력 경로")
    ap.add_argument("--title", default=None)
    a = ap.parse_args()

    now = datetime.datetime.now()
    if a.depart:
        hh, mm = map(int, a.depart.split(":"))
        depart = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    else:
        depart = now
    t = lambda m: (depart + datetime.timedelta(minutes=m)).strftime("%H:%M")

    base = naver_route(a.start, a.goal, a.force_via)
    path = base["path"]
    base_min = round(base["summary"]["duration"] / 60000)
    cum = cum_km(path)
    total = cum[-1]
    roads = {}
    for s in base["section"]:
        roads[s["name"]] = roads.get(s["name"], 0) + s["distance"]
    main_roads = [(k, round(v / 1000)) for k, v in sorted(roads.items(), key=lambda x: -x[1])[:4]]

    samples = []
    d = 0.0
    while d <= total:
        samples.append(path[min(range(len(cum)), key=lambda k: abs(cum[k] - d))])
        d += a.step
    found = {}
    with cf.ThreadPoolExecutor(6) as ex:
        for res in ex.map(lambda p: geo_search(a.query, p), samples):
            for pid, name, x, y in res:
                found[pid] = (name, float(x), float(y))

    cands = []
    for pid, (name, x, y) in found.items():
        k = min(range(0, len(path), 3), key=lambda i: km(path[i], (x, y)))
        off = km(path[k], (x, y))
        if off <= a.max_off:
            cands.append({"id": pid, "name": name, "lng": x, "lat": y, "off_km": round(off, 2), "along_km": round(cum[k]),
                          "est_min": round(base_min * cum[k] / total)})
    cands.sort(key=lambda c: c["along_km"])
    if a.window:
        lo, hi = map(int, a.window.split("-"))
        cands = [c for c in cands if lo <= c["est_min"] <= hi]
    cands = sorted(cands, key=lambda c: c["off_km"])[: a.top]
    cands.sort(key=lambda c: c["along_km"])

    def detour(c):
        wps = (a.force_via + "|" if a.force_via and c["lng"] < float(a.force_via.split("|")[0].split(",")[0]) else "") + f"{c['lng']},{c['lat']}"
        # force_via 가 후보보다 뒤(진행방향 앞)에 있으면 순서가 꼬이므로 후보만 경유
        try:
            r = naver_route(a.start, a.goal, wps)
        except SystemExit:
            r = naver_route(a.start, a.goal, f"{c['lng']},{c['lat']}")
        s = r["summary"]
        to = round(sum(w["duration"] for w in s["waypoints"]) / 60000)
        tot = round(s["duration"] / 60000)
        return {"arrive": t(to), "to_min": to, "extra_min": tot - base_min, "after_min": tot - to}

    with cf.ThreadPoolExecutor(6) as ex:
        dets = list(ex.map(detour, cands))
        infos = list(ex.map(lambda c: place_info(c["id"]), cands))
    for c, d_, i_ in zip(cands, dets, infos):
        c.update(d_)
        c.update(i_)

    result = {
        "method": f"네이버 Directions(trafast, {now.strftime('%m-%d %H:%M')} 조회) 경로 {round(total)}km {base_min}분 / 주요도로 {main_roads} / 경로 {a.step}km 간격 {len(samples)}지점 네이버플레이스 좌표검색 '{a.query}' → 경로 {a.max_off}km 이내 {len(cands)}곳 / 후보별 네이버 경유 재계산 / 영업시간·메뉴는 매장 페이지",
        "depart": depart.strftime("%H:%M"), "base_min": base_min, "base_km": round(total), "toll": base["summary"].get("tollFare"),
        "main_roads": main_roads, "candidates": cands,
    }

    if a.map:
        segs = []
        idx = 0
        for x in sorted(base["section"], key=lambda x: x["pointIndex"]):
            s0, s1 = x["pointIndex"], x["pointIndex"] + x["pointCount"]
            if s0 > idx:
                segs.append({"path": [[p[1], p[0]] for p in path[idx:s0 + 1]], "congestion": 1})
            segs.append({"path": [[p[1], p[0]] for p in path[s0:s1 + 1]], "congestion": x["congestion"]})
            idx = s1
        if idx < len(path) - 1:
            segs.append({"path": [[p[1], p[0]] for p in path[idx:]], "congestion": 1})
        segs = [{"path": g["path"][::2] if len(g["path"]) > 40 else g["path"], "congestion": g["congestion"]} for g in segs if len(g["path"]) >= 2]
        pins = [{"name": c["name"], "eta": c["arrive"], "lat": c["lat"], "lng": c["lng"],
                 "desc": " · ".join(filter(None, [f"이탈 +{c['extra_min']}분", c.get("hours"), (c["menu"][0] if c["menu"] else None)]))} for c in cands]
        pins.append({"name": "목적지", "eta": t(base_min), "lat": float(a.goal.split(",")[1]), "lng": float(a.goal.split(",")[0]), "desc": "직행 기준"})
        state = {"id": "route-scan", "title": a.title or f"{a.query} · 경로 스캔", "mylocation": True, "height": 460,
                 "routes": [{"key": "R", "label": " → ".join(k for k, _ in main_roads), "min": base_min, "km": round(total), "segments": segs}],
                 "pins": pins}
        json.dump(state, open(a.map, "w"), ensure_ascii=False)
        result["map_file"] = a.map

    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
