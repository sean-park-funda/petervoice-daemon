#!/usr/bin/env python3
"""경로 위 휴게소 검색 — 한국도로공사 공식 데이터 + 네이버 플레이스 교차검증.

사용법:
  python3 rest_areas_on_route.py --start "128.506,37.977" --goal "126.942,37.475"
  (좌표는 lng,lat / --fuel 로 각 휴게소 주유소 확인 추가)

출처 표기: EX=도로공사 공식, NAVER=네이버 검색(민자구간 등 공식 누락 보완)
"""
import argparse, json, math, os, re, sys, urllib.parse, urllib.request

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(SKILL_DIR, 'data', 'ex_rest_areas.json')
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 Version/14.0 Mobile/15E148 Safari/604.1"}
EX_API = "https://data.ex.co.kr/openapi/locationinfo/locationinfoRest?key=test&type=json&numOfRows=99&pageNo={p}"
PROXY = "https://r.jina.ai/"  # data.ex.co.kr 직접 접속이 회선에서 차단됨(2026-08-23) → 프록시 경유


def http_get(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def haversine_km(a, b):
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lng2-lng1)/2)**2
    return 6371 * 2 * math.asin(math.sqrt(h))


def refresh_official_cache():
    items, page = [], 1
    while True:
        t = http_get(PROXY + EX_API.format(p=page))
        d = json.loads(re.search(r'\{.*\}', t, re.S).group(0))
        items += d.get('list', [])
        if page * 99 >= int(d.get('count', 0)) or not d.get('list'):
            break
        page += 1
    seen, out = {}, []
    for it in items:
        k = it.get('serviceAreaCode') or it.get('unitName')
        if k in seen:
            continue
        seen[k] = 1
        out.append({'name': it['unitName'], 'route': it.get('routeName'),
                    'lng': float(it['xValue']), 'lat': float(it['yValue']), 'code': k})
    json.dump({'count': len(out), 'list': out}, open(CACHE, 'w'), ensure_ascii=False)
    return out


def load_official():
    if os.path.exists(CACHE):
        return json.load(open(CACHE))['list']
    return refresh_official_cache()


def naver_route(start, goal, via=None):
    ID, SEC = os.environ.get('NAVER_MAP_CLIENT_ID'), os.environ.get('NAVER_MAP_CLIENT_SECRET')
    if not ID:
        sys.exit("NAVER_MAP_CLIENT_ID/SECRET 필요 (source ~/.config/env/global.env)")
    url = (f"https://maps.apigw.ntruss.com/map-direction/v1/driving?start={start}&goal={goal}&option=trafast" + (f"&waypoints={via}" if via else ""))
    d = json.loads(http_get(url, {"x-ncp-apigw-api-key-id": ID, "x-ncp-apigw-api-key": SEC}))
    rt = d['route']['trafast'][0]
    return rt['path'], rt['summary']  # path: [[lng,lat],...]


def path_cumkm(path):
    cum, total = [0.0], 0.0
    for i in range(1, len(path)):
        total += haversine_km((path[i-1][1], path[i-1][0]), (path[i][1], path[i][0]))
        cum.append(total)
    return cum


def min_dist_to_path(pt, path, step=3):
    best, besti = 1e9, 0
    for i in range(0, len(path), step):
        d = haversine_km(pt, (path[i][1], path[i][0]))
        if d < best:
            best, besti = d, i
    return best, besti


def naver_search_places(query, lng, lat):
    """m.place 리스트 검색 → [{name,category,lng,lat,id}] (Apollo State 파싱)"""
    url = ("https://m.place.naver.com/place/list?" +
           urllib.parse.urlencode({'query': query, 'x': f"{lng}", 'y': f"{lat}"}))
    try:
        t = http_get(url, UA)
    except Exception:
        return []
    m = re.search(r'window\.__APOLLO_STATE__\s*=\s*({.*?});', t, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    out = []
    for v in data.values():
        if isinstance(v, dict) and v.get('name') and v.get('x') and v.get('y'):
            cat = v.get('category') or ''
            out.append({'name': v['name'], 'category': cat,
                        'lng': float(v['x']), 'lat': float(v['y']), 'id': v.get('id')})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True, help='lng,lat')
    ap.add_argument('--goal', required=True, help='lng,lat')
    ap.add_argument('--fuel', action='store_true', help='각 휴게소 주유소 존재 확인 (네이버)')
    ap.add_argument('--via', default=None, help='경로 강제용 도로 위 좌표 lng,lat (| 구분)')
    ap.add_argument('--refresh', action='store_true', help='공식 휴게소 캐시 갱신')
    ap.add_argument('--max-off-km', type=float, default=0.4, help='공식 데이터 경로 이탈 허용(km)')
    args = ap.parse_args()

    path, summary = naver_route(args.start, args.goal, args.via)
    cum = path_cumkm(path)
    official = refresh_official_cache() if args.refresh else load_official()

    found = {}
    # 1) 공식 데이터: 경로에서 400m 이내
    for ra in official:
        d, i = min_dist_to_path((ra['lat'], ra['lng']), path)
        if d <= args.max_off_km:
            found[ra['name']] = {'name': ra['name'], 'route': ra['route'], 'km': round(cum[i], 1),
                                 'lat': ra['lat'], 'lng': ra['lng'], 'source': 'EX', 'off_m': int(d*1000)}

    # 2) 네이버 교차검증: 경로 20km 간격 샘플점에서 "휴게소" 검색 (민자구간 보완)
    sample_every = 20.0
    next_at, samples = 0.0, []
    for i, c in enumerate(cum):
        if c >= next_at:
            samples.append(path[i]); next_at += sample_every
    for lng, lat in samples:
        for p in naver_search_places('휴게소', lng, lat):
            if '휴게소' not in (p['category'] or '') and '휴게소' not in p['name']:
                continue
            d, i = min_dist_to_path((p['lat'], p['lng']), path)
            if d > 0.6:
                continue
            dup = next((f for f in found.values()
                        if haversine_km((f['lat'], f['lng']), (p['lat'], p['lng'])) < 0.7), None)
            if dup:
                if p['name'] != dup['name']:
                    dup.setdefault('naver_name', p['name'])
                dup['naver_id'] = p.get('id')
                dup['source'] = 'EX+NAVER'
            elif p['name'] not in found:
                found[p['name']] = {'name': p['name'], 'route': None, 'km': round(cum[i], 1),
                                    'lat': p['lat'], 'lng': p['lng'], 'source': 'NAVER',
                                    'off_m': int(d*1000), 'naver_id': p.get('id')}

    result = sorted(found.values(), key=lambda x: x['km'])

    # 3) 주유소 확인 (옵션): 각 휴게소 좌표 800m 내 "주유소" 검색
    if args.fuel:
        for ra in result:
            gs = [p for p in naver_search_places('주유소', ra['lng'], ra['lat'])
                  if haversine_km((ra['lat'], ra['lng']), (p['lat'], p['lng'])) < 0.8
                  and '주유' in (p['category'] + p['name'])]
            ra['fuel'] = gs[0]['name'] if gs else None

    print(json.dumps({'total_km': round(summary['distance']/1000, 1),
                      'duration_min': round(summary['duration']/60000),
                      'rest_areas': result}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
