#!/usr/bin/env python3
"""의도 경유 경로 계산 + 구조적 검증 — 2026-08-23 시행착오의 재발 방지 구현.

"올림픽대로 경유로 집에 가면?" 류의 요청을 검증된 절차로 계산한다:
 1. 자유 최속 경로(기준) 계산
 2. 의도 도로가 이미 다 포함이면 그대로 반환
 3. 빠진 도로는 OSM에서 도로명+진행방향으로 앵커 추출(손좌표 금지) → 경유 재질의
 4. 구조적 검증: (a) 의도 도로 전부 section 존재 (b) 경유점 스냅 편차 <300m
    — "그럴듯한 수치"는 검증 아님. 실패는 실패로 보고한다.

사용:
  python3 route_with_via.py --start "lng,lat" --goal "lng,lat" --via "올림픽대로,경부고속도로"
출력: JSON { baseline, via_route(검증 통과시), verification }
"""
import argparse, json, math, os, sys, urllib.parse, urllib.request

OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.osm.jp/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter"]
SNAP_LIMIT_M = 300


def http(url, headers=None, data=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def hv_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 6371*2*math.asin(math.sqrt(h))


def naver_drive(start, goal, waypoints=None):
    ID, SEC = os.environ.get('NAVER_MAP_CLIENT_ID'), os.environ.get('NAVER_MAP_CLIENT_SECRET')
    if not ID:
        sys.exit("NAVER_MAP_CLIENT_ID/SECRET 필요")
    url = f"https://maps.apigw.ntruss.com/map-direction/v1/driving?start={start}&goal={goal}&option=trafast"
    if waypoints:
        url += "&waypoints=" + urllib.parse.quote("|".join(waypoints))
    d = json.loads(http(url, {"x-ncp-apigw-api-key-id": ID, "x-ncp-apigw-api-key": SEC}))
    if d.get('code') != 0:
        return None
    return d['route']['trafast'][0]


def route_summary(rt):
    s = rt['summary']
    return {'km': round(s['distance']/1000, 1), 'min': round(s['duration']/60000),
            'toll': s.get('tollFare'),
            'sections': [{'name': x['name'], 'km': round(x['distance']/1000, 1),
                          'congestion': x['congestion'], 'speed': x['speed']} for x in rt['section']]}


def roads_in_route(rt):
    return set(x['name'] for x in rt.get('section', []))


def missing_roads(rt, via):
    have = roads_in_route(rt)
    return [v for v in via if not any(v in h or h in v for h in have)]


def bearing(a, b):  # (lat,lng)
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    dlo = math.radians(b[1]-a[1])
    y = math.sin(dlo)*math.cos(la2)
    x = math.cos(la1)*math.sin(la2)-math.sin(la1)*math.cos(la2)*math.cos(dlo)
    return math.degrees(math.atan2(y, x)) % 360


def osm_anchors(road_name, bbox, want_bearing):
    """도로명으로 OSM way 조회 → 진행방향이 want_bearing과 맞는 way들의 중간점 후보 반환."""
    q = f'[out:json][timeout:20];way["name"="{road_name}"]["highway"]({bbox});out tags geom;'
    raw = None
    for ep in OVERPASS:
        try:
            t = http(ep, data=urllib.parse.urlencode({'data': q}).encode(), timeout=30)
            if t.lstrip().startswith('{'):
                raw = json.loads(t)
                break
        except Exception:
            continue
    if not raw:
        return []
    cands = []
    for w in raw.get('elements', []):
        g = w.get('geometry', [])
        if len(g) < 5:
            continue
        a, b = (g[0]['lat'], g[0]['lon']), (g[-1]['lat'], g[-1]['lon'])
        br = bearing(a, b)
        diff = min(abs(br-want_bearing), 360-abs(br-want_bearing))
        oneway = w.get('tags', {}).get('oneway') == 'yes'
        # oneway 는 방향 90도 이내만, 양방향 도로는 방향 무관 허용
        if oneway and diff > 90:
            continue
        mid = g[len(g)//2]
        cands.append({'pt': (mid['lat'], mid['lon']), 'bearing_diff': round(diff), 'pts': len(g)})
    cands.sort(key=lambda c: (c['bearing_diff'], -c['pts']))
    return cands[:4]


def snap_deviation_m(rt, wp_latlng):
    path = rt['path']
    best = min(hv_km(wp_latlng, (p[1], p[0])) for p in path[::2])
    return int(best*1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True)  # lng,lat
    ap.add_argument('--goal', required=True)
    ap.add_argument('--via', required=True, help='경유 도로명 콤마 구분 (예: "올림픽대로,경부고속도로")')
    args = ap.parse_args()
    via = [v.strip() for v in args.via.split(',') if v.strip()]
    slng, slat = map(float, args.start.split(','))
    glng, glat = map(float, args.goal.split(','))

    out = {'via_requested': via}
    base = naver_drive(args.start, args.goal)
    if not base:
        sys.exit("기준 경로 계산 실패")
    out['baseline'] = route_summary(base)

    miss = missing_roads(base, via)
    if not miss:
        out['via_route'] = out['baseline']
        out['verification'] = {'ok': True, 'note': '자유 최속 경로가 이미 의도 도로를 모두 포함'}
        print(json.dumps(out, ensure_ascii=False, indent=1)); return

    # 빠진 도로마다 OSM 앵커 → 후보 조합 시도 (도로당 후보 4개, 순차)
    want_b = bearing((slat, slng), (glat, glng))
    pad = 0.05
    bbox = f"{min(slat,glat)-pad},{min(slng,glng)-pad},{max(slat,glat)+pad},{max(slng,glng)+pad}"
    anchor_sets = {}
    for road in miss:
        cands = osm_anchors(road, bbox, want_b)
        if not cands:
            out['verification'] = {'ok': False, 'note': f'OSM에서 "{road}" 앵커 확보 실패 (서버 불가 또는 미등재) — 손좌표로 대체하지 않음'}
            print(json.dumps(out, ensure_ascii=False, indent=1)); return
        anchor_sets[road] = cands

    attempts = []
    best_rt, best_wps = None, None
    for i in range(max(len(c) for c in anchor_sets.values())):
        wps, wp_pts = [], []
        for road in miss:
            c = anchor_sets[road][min(i, len(anchor_sets[road])-1)]
            wps.append(f"{c['pt'][1]},{c['pt'][0]}")
            wp_pts.append(c['pt'])
        rt = naver_drive(args.start, args.goal, wps)
        if not rt:
            attempts.append({'waypoints': wps, 'fail': 'API 오류'}); continue
        still = missing_roads(rt, via)
        devs = [snap_deviation_m(rt, p) for p in wp_pts]
        rec = {'waypoints': wps, 'missing_after': still, 'snap_dev_m': devs}
        attempts.append(rec)
        if not still and max(devs) <= SNAP_LIMIT_M:
            best_rt, best_wps = rt, wps
            break

    out['attempts'] = attempts
    if best_rt:
        out['via_route'] = route_summary(best_rt)
        out['verification'] = {'ok': True, 'checks': {
            '의도도로_전부_포함': True,
            '스냅편차_300m이내': True,
            'waypoints': best_wps}}
    else:
        out['verification'] = {'ok': False,
            'note': '구조 검증을 통과한 경유 경로를 만들지 못함 — 수치가 그럴듯해도 반환하지 않음'}
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
