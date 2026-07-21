"""
메타 광고 자동 동기화 스크립트
사용법: python meta_sync.py
"""

import requests, json, re, os, sys, glob, time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ── 설정 ──────────────────────────────────────────
AD_ACCOUNT_ID = "act_863491278417905"
API_VERSION   = "v21.0"
BASE_URL      = f"https://graph.facebook.com/{API_VERSION}"

# GitHub 자동 push 설정 (선택사항)
GITHUB_REPO   = "richardahn-stack/dashboard001"   # 본인 레포
OUTPUT_DIR    = "./data"                           # JSON 저장 경로

# 드라이브 맵 경로 (로컬 실행 시 fallback용)
DRIVE_MAP_PATH = "./drive_map.json"

# ── 소재 분류 로직 ────────────────────────────────
SKIP = {'공식','DT','dt','영상','배너','콘조','오딧','플랩','핑크프로모션',
        '전환','상시','2차활용','카탈로그','트래픽','핑크사전','웨딩프로모션'}

def get_landing(camp, ad):
    if any(k in camp for k in ['아이시핑크','핑크프로모션','핑크사전']): return '핑크'
    if '웨딩' in camp: return '웨딩'
    if '플랩' in ad and '오딧' not in ad: return '플랩'
    if '오딧' in ad and '플랩' not in ad: return '오딧'
    if '플랩' in camp and '오딧' not in camp: return '플랩'
    if '오딧' in camp and '플랩' not in camp: return '오딧'
    if '오딧' in camp and '플랩' in camp: return '오딧+플랩'
    return '기타'

def classify(camp, ad):
    if '트래픽' in camp: obj = '트래픽'
    elif '콘텐츠조회' in camp: obj = '콘조'
    else: obj = '전환'
    pa = 'PA' if obj=='전환' and 'PA' in camp else ('자컨' if obj=='전환' else '-')
    return obj, pa, get_landing(camp, ad)

def assign_mgr(name):
    if '파트너스' in name or '인스타툰' in name: return '파트너스'
    parts = name.split('_')
    nd = [p for p in parts if not re.match(r'^\d{6}$', p)]
    last = nd[-1].strip() if nd else ''
    for m in ['리지','콜리','세레나']:
        if last == m: return m
    return '기타'

def get_media(name): return '배너' if '배너' in name else '영상'

def extract_bk(name):
    parts = name.split('_')
    return '_'.join([p for p in parts if not re.match(r'^\d{6}$',p) and p not in SKIP])

def get_age(name, ref):
    ms = re.findall(r'(?:^|_)(\d{6})(?:_|$|\.)', name)
    if not ms: ms = re.findall(r'(\d{6})', name)
    if not ms: return None
    try:
        d = datetime.strptime('20'+ms[-1], '%Y%m%d')
        days = (ref - d).days
        return int(days) if days >= 0 else None
    except: return None


# ── 드라이브 맵 fallback ──────────────────────────
def load_drive_map():
    """드라이브 맵 로드 (없으면 빈 dict 반환)"""
    try:
        with open(DRIVE_MAP_PATH, 'r') as f:
            dm = json.load(f)
        img_exact = {tuple(k.split('||')): v for k, v in dm.get('img_exact', {}).items()}
        img_fb    = dm.get('img_fallback', {})
        vid_exact = {tuple(k.split('||')): v for k, v in dm.get('vid_exact', {}).items()}
        # suffix fallback용 인덱스
        bks_by_land = {}
        for k in list(dm.get('vid_exact',{}).keys()) + list(dm.get('img_exact',{}).keys()):
            bk, land = k.split('||')
            bks_by_land.setdefault(land, [])
            if bk not in bks_by_land[land]:
                bks_by_land[land].append(bk)
        return img_exact, img_fb, vid_exact, bks_by_land
    except Exception:
        return {}, {}, {}, {}

def drive_thumb(bk, land, media, img_exact, img_fb, vid_exact, bks_by_land):
    """드라이브 맵에서 썸네일 ID 검색 (suffix fallback 포함)"""
    def suffix_match(bk, land):
        for dbk in bks_by_land.get(land, []):
            if bk.endswith(dbk) and bk != dbk:
                return dbk
        return None
    vid = vid_exact.get((bk, land))
    img = img_exact.get((bk, land)) or img_fb.get(bk)
    if not vid and not img:
        mbk = suffix_match(bk, land)
        if mbk:
            vid = vid_exact.get((mbk, land))
            img = img_exact.get((mbk, land)) or img_fb.get(mbk)
    if media == '영상':
        if vid: return vid, 'video'
        if img: return img, 'img'
    else:
        if img: return img, 'img'
        if vid: return vid, 'video'
    return None, None

# 드라이브 맵 전역 로드
_dm_img_exact, _dm_img_fb, _dm_vid_exact, _dm_bks_by_land = load_drive_map()

# ── 메타 API 호출 ─────────────────────────────────
def api_get(path, params, token):
    params['access_token'] = token
    r = requests.get(f"{BASE_URL}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def paginate(path, params, token):
    """페이지네이션 자동 처리"""
    results = []
    data = api_get(path, params, token)
    results.extend(data.get('data', []))
    while 'paging' in data and 'next' in data['paging']:
        r = requests.get(data['paging']['next'], timeout=30)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get('data', []))
    return results


def fetch_creative_thumb(ad_id, token):
    """단일 광고의 최신 썸네일 URL 조회 (/{ad_id}/adcreatives).
    메타는 호출할 때마다 새로 서명된 URL(새 oe=)을 돌려줌.
    반환: (thumb_url, img_type, video_id)  — 소재 없거나 조회 실패 시 thumb_url=None"""
    cr_list = api_get(
        f"{ad_id}/adcreatives",
        {'fields': 'thumbnail_url,image_url,video_id,object_story_spec,asset_feed_spec,picture'},
        token
    )
    cr_data = cr_list.get('data', [])
    cr = cr_data[0] if cr_data else {}
    video_id = cr.get('video_id')
    if video_id:
        img_type = 'video'
        thumb_url = (
            cr.get('object_story_spec', {}).get('video_data', {}).get('image_url')
            or (cr.get('asset_feed_spec', {}).get('videos') or [{}])[0].get('thumbnail_url')
            or cr.get('picture')
            or cr.get('thumbnail_url')
        )
    else:
        img_type = 'img'
        thumb_url = (cr.get('image_url')
                     or cr.get('picture')
                     or cr.get('object_story_spec', {}).get('link_data', {}).get('image_url')
                     or cr.get('object_story_spec', {}).get('link_data', {}).get('picture'))
        if not thumb_url:
            imgs = cr.get('asset_feed_spec', {}).get('images', [])
            if imgs:
                thumb_url = imgs[0].get('url')
    return thumb_url, img_type, video_id


def fetch_week_data(token, date_start, date_end):
    """한 주차 광고 성과 + 소재 정보 수집"""
    print(f"  📡 {date_start} ~ {date_end} 데이터 수집 중...")

    # 1. 광고 인사이트 (성과 데이터)
    insights = paginate(
        f"{AD_ACCOUNT_ID}/insights",
        {
            'level': 'ad',
            'time_range': json.dumps({'since': date_start, 'until': date_end}),
            'fields': ','.join([
                'ad_id', 'ad_name', 'campaign_name',
                'spend', 'impressions', 'clicks', 'ctr', 'cpc',
                'actions', 'action_values', 'purchase_roas',
            ]),
            'limit': 500,
        },
        token
    )
    print(f"    광고 {len(insights)}개 수집")

    # 2. 광고 소재 정보 (메타 API 전용 - 드라이브 불필요)
    ad_ids = [i['ad_id'] for i in insights]
    creatives = {}

    for ad_id in ad_ids:
        if not ad_id:
            continue
        try:
            thumb_url, img_type, video_id = fetch_creative_thumb(ad_id, token)
            creatives[ad_id] = {
                'thumbnail_url': thumb_url,
                'video_id':      video_id,
                'video_source':  None,  # mp4 직접재생 불가 (ads_read 권한 제한)
                'type':          img_type,
            }
        except Exception as e:
            print(f"    ⚠️  크리에이티브 수집 실패 [{ad_id}]: {e}")
            creatives[ad_id] = {'thumbnail_url': None, 'video_id': None, 'video_source': None, 'type': None}
    found = sum(1 for cr in creatives.values() if cr.get('thumbnail_url'))
    print(f"    소재 {len(creatives)}개 수집 (썸네일 {found}개 확보)")
    return insights, creatives



def calc_trends(rows, avg_rev):
    """전주 대비 트렌드 계산"""
    valid = [r for r in rows if r.get('prev_spend') and r['spend'] >= 50000]

    # 광고비 상승
    su = sorted(
        [{**r, '_v': r['spend']-r['prev_spend'], '_p': (r['spend']-r['prev_spend'])/r['prev_spend']*100}
         for r in valid if r['spend'] > r['prev_spend']],
        key=lambda x: x['_v'], reverse=True)[:5]

    # 매출 상승
    ru = []
    for r in valid:
        pr = r.get('prev_revenue') or 0
        cr = r.get('revenue') or 0
        if pr > 0 and cr > pr:
            ab = cr - pr; pt = (cr-pr)/pr*100
            ru.append({**r, '_v': ab, '_p': pt, '_score': (ab/max(avg_rev,1))+(pt/100)})
    ru = sorted(ru, key=lambda x: x['_score'], reverse=True)[:5]

    # CPC 효율 상승 (CPC 하락)
    cu = []
    for r in valid:
        pc = r.get('prev_cpc') or 0; cc = r.get('cpc') or 0
        if pc > 0 and cc > 0 and cc < pc:
            cu.append({**r, '_v': cc, '_p': -(pc-cc)/pc*100, '_drop': (pc-cc)/pc*100})
    cu = sorted(cu, key=lambda x: x['_drop'], reverse=True)[:5]

    # 전환 하락
    cd = []
    for r in valid:
        if r['spend'] < 100000: continue
        pr = r.get('old_roas_pct') or 0; crv = r.get('roas_pct') or 0
        pc = r.get('prev_cvr') or 0; cc = r.get('cvr') or 0
        if pr > 0 and pc > 0 and pr > crv and pc > cc:
            rd = (pr-crv)/pr*100; cvd = (pc-cc)/pc*100
            cd.append({**r, '_v': crv, '_p': -rd, '_roas_drop': rd, '_cvr_drop': cvd, '_score': (rd+cvd)/2})
    cd = sorted(cd, key=lambda x: x['_score'], reverse=True)[:5]

    # CPC 효율 하락 (CPC 상승)
    cdn = []
    for r in valid:
        pc = r.get('prev_cpc') or 0; cc = r.get('cpc') or 0
        if pc > 0 and cc > pc:
            cdn.append({**r, '_v': cc, '_p': (cc-pc)/pc*100, '_rise': (cc-pc)/pc*100})
    cdn = sorted(cdn, key=lambda x: x['_rise'], reverse=True)[:5]

    return {'spend_up': su, 'rev_up': ru, 'cpc_eff_up': cu, 'conv_dn': cd, 'cpc_eff_dn': cdn}

def build_json_from_api(token, end_date_str, prev_end_date_str, label, prev_label):
    """메타 API 데이터로 JSON 빌드"""
    ref = datetime.strptime(end_date_str, '%Y-%m-%d')
    # 이번 주 (7일)
    start = (ref - timedelta(days=6)).strftime('%Y-%m-%d')
    # 전주
    prev_end = datetime.strptime(prev_end_date_str, '%Y-%m-%d')
    prev_start = (prev_end - timedelta(days=6)).strftime('%Y-%m-%d')

    insights, creatives = fetch_week_data(token, start, end_date_str)
    prev_insights, _ = fetch_week_data(token, prev_start, prev_end_date_str)

    def parse_insights(ins_list):
        rows = {}
        for ins in ins_list:
            ad_name = ins.get('ad_name', '')
            camp_name = ins.get('campaign_name', '')
            if not ad_name: continue

            spend = float(ins.get('spend', 0))
            if spend <= 0: continue

            impressions = int(ins.get('impressions', 0))
            clicks = int(ins.get('clicks', 0))
            ctr = float(ins.get('ctr', 0))
            cpc = float(ins.get('cpc', 0))

            # 전환/구매 추출
            actions = {a['action_type']: float(a['value'])
                       for a in ins.get('actions', [])}
            action_vals = {a['action_type']: float(a['value'])
                           for a in ins.get('action_values', [])}
            purchases = actions.get('purchase', 0)
            revenue = action_vals.get('purchase', 0)
            roas_list = ins.get('purchase_roas', [])
            roas = float(roas_list[0]['value']) if roas_list else 0

            obj, pa, land = classify(camp_name, ad_name)
            bk = extract_bk(ad_name)
            key = f"{obj}_{bk}_{land}"
            mgr = assign_mgr(ad_name)
            media = get_media(ad_name)
            age = get_age(ad_name, ref)

            if key not in rows:
                rows[key] = {
                    'key': key, 'base_key': bk,
                    'ad_id': ins.get('ad_id'), '_max_spend': spend,
                    'objective': obj, 'pa_type': pa, 'landing': land,
                    'purpose': obj, 'mgr': mgr, 'media': media,
                    'promo': ('썸머블루위크' in ad_name),
                    'spend': 0, 'impressions': 0, 'clicks': 0,
                    'purchases': 0, 'revenue': 0, 'roas_sum': 0, 'roas_n': 0,
                    'age_days': age,
                }
            else:
                # 기존 row: spend 더 높은 광고의 ad_id로 업데이트
                if spend > rows[key].get('_max_spend', 0):
                    rows[key]['ad_id'] = ins.get('ad_id')
                    rows[key]['_max_spend'] = spend
            r = rows[key]
            r['spend'] += spend
            r['impressions'] += impressions
            r['clicks'] += clicks
            r['purchases'] += purchases
            r['revenue'] += revenue
            if roas > 0: r['roas_sum'] += roas; r['roas_n'] += 1

        # 집계 후 지표 계산
        result = []
        for r in rows.values():
            r['roas'] = r['roas_sum'] / r['roas_n'] if r['roas_n'] else 0
            r['roas_pct'] = round(r['roas'] * 100, 0)
            r['ctr'] = round(r['clicks'] / r['impressions'] * 100, 2) if r['impressions'] else 0
            r['cvr'] = round(r['purchases'] / r['clicks'] * 100, 2) if r['clicks'] else 0
            r['cpc'] = round(r['spend'] / r['clicks'], 0) if r['clicks'] else 0
            r['spend'] = int(r['spend'])
            r['revenue'] = int(r['revenue'])
            r['is_new'] = (r['age_days'] is not None and r['age_days'] <= 6)
            del r['roas_sum'], r['roas_n']
            r.pop('_max_spend', None)
            result.append(r)
        return result

    rows = parse_insights(insights)
    prev_rows = parse_insights(prev_insights)
    prev_map = {r['key']: r for r in prev_rows}

    # 전주 대비 지표 추가
    conv = [r for r in rows if r['objective']=='전환' and r['spend']>=100000]
    # ROAS: 메타 방식 = 총 전환값 / 총 광고비 (개별 평균 아님)
    _conv_rev   = sum(r['revenue'] for r in conv)
    _conv_spend = sum(r['spend']   for r in conv)
    avg_roas = _conv_rev / _conv_spend if _conv_spend else 0

    # CTR/CVR/CPC/구매: 전환 소재 전체 산술평균
    avg_ctr  = sum(r['ctr']  for r in conv)/len(conv) if conv else 0
    avg_cvr  = sum(r['cvr']  for r in conv)/len(conv) if conv else 0
    avg_cpc  = sum(r['cpc']  for r in conv if r['cpc']>0)/len([r for r in conv if r['cpc']>0]) if conv else 0
    avg_pur  = sum(r['purchases'] for r in conv)/len(conv) if conv else 0
    tc_avg_ctr = sum(r['ctr'] for r in rows)/len(rows) if rows else 0

    final_rows = []
    for r in rows:
        old = prev_map.get(r['key'], {})
        old_roas = old.get('roas')
        roas_delta = round((r['roas']-old_roas)/old_roas*100,1) if old_roas and old_roas>0 and r['roas']>0 else None

        # 위닝 판정 (spend 기준 제거 — 신규 소재도 평가)
        ws = None; wf = {}; wt = None
        if r['objective']=='전환' and avg_roas>0:
            wf = {
                'roas': r['roas']>avg_roas, 'ctr': r['ctr']>avg_ctr,
                'cvr': r['cvr']>avg_cvr, 'cpc': r['cpc']>0 and r['cpc']<avg_cpc,
                'purchases': r['purchases']>avg_pur
            }
            ws = sum(wf.values()); wt = '전환'
        elif r['objective'] in ['트래픽','콘조']:
            wf = {'ctr': r['ctr']>tc_avg_ctr}
            ws = 1 if wf['ctr'] else 0; wt = 'tc'

        # 소재 썸네일 + 영상 소스 URL
        ad_id = r.get('ad_id')
        cr = creatives.get(ad_id, {})
        thumb_url = cr.get('thumbnail_url')
        video_id  = cr.get('video_id')
        img_type  = cr.get('type')



        final_rows.append({
            **r,
            'ad_id': r.get('ad_id'),
            'old_roas_pct': round(old_roas*100,0) if old_roas else None,
            'roas_delta': roas_delta,
            'prev_spend': int(old['spend']) if old.get('spend') else None,
            'prev_ctr': round(old['ctr'],2) if old.get('ctr') else None,
            'prev_cvr': round(old['cvr'],2) if old.get('cvr') else None,
            'prev_cpc': round(old['cpc'],0) if old.get('cpc') else None,
            'prev_purchases': int(old['purchases']) if old.get('purchases') else None,
            'prev_revenue': int(old['revenue']) if old.get('revenue') else None,
            'img': thumb_url,
            'img_type': img_type,
            'video': video_id,
            'video_src': cr.get('video_source'),  # mp4 직접 재생 URL
            'win_score': ws, 'win_type': wt, 'win_flags': wf,
        })

    # 요약
    total_spend = sum(r['spend'] for r in final_rows)
    summary = {
        'spend': total_spend,
        'impressions': sum(r['impressions'] for r in final_rows),
        'clicks': sum(r['clicks'] for r in final_rows),
        'purchases': int(sum(r['purchases'] for r in final_rows)),
        'revenue': int(sum(r['revenue'] for r in final_rows)),
        'roas_pct': round(sum(r['revenue'] for r in final_rows)/total_spend*100,1) if total_spend else 0,
        'avg_ctr': round(sum(r['ctr'] for r in final_rows)/len(final_rows),2) if final_rows else 0,
        'avg_cpc': round(sum(r['cpc'] for r in final_rows if r['cpc']>0)/len([r for r in final_rows if r['cpc']>0]),0) if final_rows else 0,
        'active_ads': len(insights),
        'winning_thresholds': {
            'avg_roas': round(avg_roas,3), 'avg_roas_pct': round(avg_roas*100,1),
            'avg_ctr': round(avg_ctr,2), 'avg_cvr': round(avg_cvr,2),
            'avg_cpc': round(avg_cpc,0), 'avg_purchases': round(avg_pur,1),
            'tc_avg_ctr': round(tc_avg_ctr,2)
        }
    }

    avg_rev = sum(r['revenue'] for r in final_rows) / len(final_rows) if final_rows else 1
    all_ads = sorted(final_rows, key=lambda x: x['spend'], reverse=True)
    conv_win = sorted([r for r in final_rows if r['win_type']=='전환' and (r['win_score'] or 0)>=3],
                      key=lambda x: x['revenue'], reverse=True)[:10]
    tc_win   = sorted([r for r in final_rows if r['win_type']=='tc' and (r['win_score'] or 0)>=1],
                      key=lambda x: x['ctr'], reverse=True)[:10]
    d7_all   = [r for r in final_rows if r['age_days'] is not None and r['age_days']<=7 and r['spend']>=30000]
    # 위닝: 전환 win_score>=3 또는 tc win_score>=1, 정렬: score↓ → spend↓
    d7_win   = sorted(
        [r for r in d7_all if (r['win_type']=='전환' and (r['win_score'] or 0)>=3)
                           or (r['win_type']=='tc'   and (r['win_score'] or 0)>=1)],
        key=lambda x: (-(x['win_score'] or 0), -x['spend'])
    )
    # 루징: 위닝 제외 전체, 정렬: spend↓
    d7_win_ids = {id(r) for r in d7_win}
    d7_lose  = sorted(
        [r for r in d7_all if id(r) not in d7_win_ids],
        key=lambda x: -x['spend']
    )

    prev_total = sum(r['spend'] for r in prev_rows)
    # 이전 주차 전환 소재 평균 (메타 방식 ROAS)
    prev_conv = [r for r in prev_rows if r.get('objective') == '전환']
    prev_conv_rev   = sum(r['revenue'] for r in prev_conv)
    prev_conv_spend = sum(r['spend']   for r in prev_conv)
    prev_avg_roas_pct = round(prev_conv_rev / prev_conv_spend * 100, 1) if prev_conv_spend else 0
    prev_avg_ctr  = round(sum(r['ctr'] for r in prev_conv) / len(prev_conv), 2) if prev_conv else 0
    prev_avg_cvr  = round(sum(r['cvr'] for r in prev_conv) / len(prev_conv), 2) if prev_conv else 0
    prev_cpc_rows = [r for r in prev_conv if r.get('cpc', 0) > 0]
    prev_avg_cpc  = round(sum(r['cpc'] for r in prev_cpc_rows) / len(prev_cpc_rows), 0) if prev_cpc_rows else 0
    prev_s = {
        'spend': int(prev_total),
        'revenue': int(sum(r['revenue'] for r in prev_rows)),
        'roas_pct': round(sum(r['revenue'] for r in prev_rows)/prev_total*100,1) if prev_total else 0,
        'purchases': int(sum(r['purchases'] for r in prev_rows)),
        'impressions': int(sum(r['impressions'] for r in prev_rows)),
        'clicks': int(sum(r['clicks'] for r in prev_rows)),
        'avg_ctr': prev_avg_ctr,
        'avg_cvr': prev_avg_cvr,
        'avg_cpc': prev_avg_cpc,
        'avg_roas_pct': prev_avg_roas_pct,
        'active_ads': len(prev_insights),
    }

    return {
        'label': label, 'prev_label': prev_label, 'ref_date': end_date_str,
        'summary': summary, 'prev_summary': prev_s,
        'trends': calc_trends(final_rows, avg_rev), 'pie': {},
        'winning': conv_win + tc_win,
        'd7_win': d7_win, 'd7_lose': d7_lose,
        'all_ads': all_ads,
    }


# ── index.html 썸네일 렌더러 패치 ────────────────
# 기존: <img src="drive.google.com/thumbnail?id=...">
# 변경: <img src="[메타 썸네일 URL]">
# → tThumb 함수가 img 필드를 URL로 직접 쓰도록 index.html 수정 필요
THUMB_PATCH_NOTE = """
⚠️  index.html 수정 필요:
tThumb 함수에서 드라이브 URL 대신 직접 URL 사용하도록 변경
현재: https://drive.google.com/thumbnail?id=${img}&sz=w400
변경: ${img}  (메타 API가 이미 완전한 URL을 반환)
"""


# ── 메인 실행 ─────────────────────────────────────
def get_leader_top7_keys(json_path):
    """JSON 파일에서 리딩 소재 상위 7개 base_key 반환"""
    try:
        with open(json_path, encoding='utf-8') as f:
            d = json.load(f)
        conv = [r for r in d.get('all_ads', [])
                if r.get('objective') == '전환' and r.get('spend', 0) > 0]
        top7 = sorted(conv, key=lambda x: -x['spend'])[:7]
        return {r['base_key'] for r in top7}
    except Exception:
        return set()


def calc_leader_badges(current_keys, weeks_in_order, output_dir):
    """소재별 뱃지 계산 (new / n주연속 / comeback)
    weeks_in_order: 최신→과거 순 week dict 리스트
    현재 주차는 weeks_in_order[0]
    """
    badges = {}
    # 과거 주차 키셋 목록 (최신→과거, 현재 제외)
    past_keysets = []
    for w in weeks_in_order[1:]:
        path = f"{output_dir}/{w['id']}.json"
        past_keysets.append(get_leader_top7_keys(path))

    for key in current_keys:
        # 연속 등장 횟수
        streak = 1
        for ks in past_keysets:
            if key in ks:
                streak += 1
            else:
                break
        # 과거 전체 이력 (연속 끊긴 이후에도 있었는지)
        has_history = any(key in ks for ks in past_keysets)

        if streak >= 2:
            badges[key] = {'type': 'streak', 'count': streak}
        elif has_history:
            badges[key] = {'type': 'comeback'}
        else:
            badges[key] = {'type': 'new'}
    return badges


# ── 썸네일 링크 갱신 패스 (만료 임박분만 새 링크로 교체) ──────────────
# 방침: 이미지 파일을 저장하지 않고 URL만 갱신 → 서버 용량 0.
#   단, 소재가 삭제돼 메타가 URL을 안 주면 그 소재는 그대로 둠(포기).
#   → 이 스크립트가 만료 창(짧게는 ~10일)보다 자주 실행돼야 링크가 안 죽음.
#     (매일/매주 Actions면 충분)

def _needs_refresh(url, margin_days=7):
    """만료가 margin_days 이내로 임박했거나 이미 지난 scontent...&oe= URL이면 True.
    www.facebook.com/ads/image/?d= 형태(만료 없음)나 우리 경로는 False."""
    if not url or 'fbcdn.net' not in url or 'oe=' not in url:
        return False
    m = re.search(r'[?&]oe=([0-9A-Fa-f]+)', url)
    if not m:
        return False
    try:
        expiry = int(m.group(1), 16)          # oe = unix timestamp (16진수)
    except ValueError:
        return False
    return expiry <= time.time() + margin_days * 86400


def _iter_ad_entries(obj):
    """JSON 구조 어디에 있든 광고 엔트리(dict with 'ad_id'&'img')를 모두 순회."""
    if isinstance(obj, dict):
        if 'ad_id' in obj and 'img' in obj:
            yield obj
        for v in obj.values():
            yield from _iter_ad_entries(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_ad_entries(v)


def refresh_expiring_thumbs(token, output_dir, margin_days=7):
    """과거 주차 JSON들을 훑어 만료 임박 썸네일 URL만 새 링크로 교체."""
    print("\n" + "=" * 50)
    print("  🔄 썸네일 링크 갱신 (만료 임박분)")
    print("=" * 50)

    files = [f for f in sorted(glob.glob(f"{output_dir}/*.json"))
             if os.path.basename(f) not in ('weeks.json', 'sales.json')]

    # 1) 만료 임박 엔트리 수집 (파일별 보관 + 대상 ad_id 집합)
    file_entries = {}            # path -> (data, [엔트리들])
    ad_ids_needed = set()
    for path in files:
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            print(f"  ⚠️  {os.path.basename(path)} 읽기 실패: {e}")
            continue
        need = [e for e in _iter_ad_entries(data)
                if e.get('ad_id') and _needs_refresh(e.get('img'), margin_days)]
        if need:
            file_entries[path] = (data, need)
            ad_ids_needed.update(e['ad_id'] for e in need)

    if not ad_ids_needed:
        print("  ✅ 만료 임박 썸네일 없음 — 갱신 불필요")
        return

    print(f"  대상 소재 {len(ad_ids_needed)}개 · 파일 {len(file_entries)}개")

    # 2) ad_id별 새 URL 1회씩 조회 (캐시). 삭제/조회불가 소재는 None(=포기)
    fresh, ok, dead = {}, 0, 0
    for ad_id in ad_ids_needed:
        try:
            url, _t, _v = fetch_creative_thumb(ad_id, token)
            fresh[ad_id] = url
            if url:
                ok += 1
            else:
                dead += 1
        except Exception as e:
            fresh[ad_id] = None
            dead += 1
            print(f"    ⚠️  갱신 실패(삭제/조회불가) [{ad_id}]: {e}")
    print(f"  새 링크 확보 {ok}개 / 실패·포기 {dead}개")

    # 3) 파일별로 img 덮어쓰기 후 저장 (실제 변경분 있을 때만)
    updated = 0
    for path, (data, entries) in file_entries.items():
        changed = 0
        for e in entries:
            new = fresh.get(e['ad_id'])
            if new:
                e['img'] = new
                changed += 1
        if changed:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            updated += 1
            print(f"    ♻️  {os.path.basename(path)}: {changed}개 URL 갱신")
    print(f"  ✅ 갱신 완료: 파일 {updated}개 수정")


def main():
    print("=" * 50)
    print("  메타 광고 대시보드 자동 동기화")
    print("=" * 50)

    # 환경변수 우선 (GitHub Actions), 없으면 직접 입력 (로컬 실행)
    token = os.environ.get('META_TOKEN', '').strip()
    if not token:
        token = input("\n시스템 사용자 액세스 토큰을 입력하세요: ").strip()
    if not token:
        print("❌ 토큰이 없습니다. META_TOKEN 환경변수를 설정하거나 직접 입력하세요.")
        sys.exit(1)

    # 토큰 유효성 검증
    print("\n🔍 토큰 확인 중...")
    try:
        me = api_get('me', {'fields': 'name,id'}, token)
        print(f"✅ 토큰 유효: {me.get('name', me.get('id'))}")
    except Exception as e:
        print(f"❌ 토큰 오류: {e}")
        sys.exit(1)

    # 광고 계정 확인
    print(f"\n🔍 광고 계정 확인 중: {AD_ACCOUNT_ID}")
    try:
        acc = api_get(AD_ACCOUNT_ID, {'fields': 'name,currency,account_status'}, token)
        print(f"✅ 계정 확인: {acc.get('name')} ({acc.get('currency')})")
    except Exception as e:
        print(f"❌ 계정 오류: {e}")
        sys.exit(1)

    # 수집할 주차 설정 (W19부터 현재까지)
    # ⚠️ GitHub Actions 서버는 UTC라 datetime.today()는 한국시간보다 최대 9시간 느림.
    #    그러면 한국 월요일 오전인데 서버는 아직 일요일 → 막 끝난 주차가 누락됨.
    #    한국시간(KST, UTC+9) 기준 날짜로 계산해야 주차 경계가 맞음.
    KST = timezone(timedelta(hours=9))
    today = datetime.now(KST).replace(tzinfo=None)
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)

    # 시작 기준: 2025-04-06 (일요일) — 과거 주차 소급 생성 (4월까지 백필)
    START_SUNDAY = datetime(2025, 4, 6)

    weeks = []
    cur = last_sunday
    while cur >= START_SUNDAY:
        start = cur - timedelta(days=6)
        label = (f"{start.month}/{start.day}~{cur.day}" if start.month==cur.month
                 else f"{start.month}/{start.day}~{cur.month}/{cur.day}")
        # 완료된 주차만 포함 (end=일요일이 오늘보다 과거여야 함)
        # 즉 오늘이 주 중간이면 이번 주는 아직 미완료 → JSON 미생성
        if cur.date() < today.date():
            weeks.append({
                'id':    cur.strftime('%Y-W%U'),
                'label': label,
                'end':   cur.strftime('%Y-%m-%d'),
                'start': start.strftime('%Y-%m-%d'),
            })
        cur -= timedelta(weeks=1)

    print(f"\n📅 수집 대상 주차: {len(weeks)}개")
    for w in weeks:
        print(f"   {w['label']} ({w['start']} ~ {w['end']})")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    weeks_list = []

    for i, w in enumerate(weeks):
        print(f"\n[{i+1}/{len(weeks)}] {w['label']} 처리 중...")
        out_path = f"{OUTPUT_DIR}/{w['id']}.json"
        # 이번 주(i=0)는 항상 갱신, 이전 주는 파일 있으면 skip (매주 자동 모드)
        if i > 0 and os.path.exists(out_path):
            print(f"  ⏭️  {out_path} 이미 존재 → skip")
            weeks_list.append({'id': w['id'], 'label': w['label'], 'ref_date': w['end']})
            continue
        try:
            prev = weeks[i+1] if i+1 < len(weeks) else weeks[i]
            data = build_json_from_api(
                token,
                w['end'], prev['end'],
                w['label'], prev['label']
            )
            # 리딩 소재 뱃지 계산 (이전 주차 파일 참조)
            cur_keys = {r['base_key'] for r in
                        sorted([r for r in data['all_ads']
                                if r.get('objective')=='전환' and r.get('spend',0)>0],
                               key=lambda x: -x['spend'])[:7]}
            data['leader_badges'] = calc_leader_badges(cur_keys, weeks[i:], OUTPUT_DIR)
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            print(f"  ✅ {out_path} 저장 완료 (소재 {len(data['all_ads'])}개)")
            weeks_list.append({'id': w['id'], 'label': w['label'], 'ref_date': w['end']})
        except Exception as e:
            print(f"  ❌ 오류: {e}")
            import traceback; traceback.print_exc()

    # ── 안전장치: 이번 실행에서 실패/누락된 주차라도
    #    기존 JSON 파일이 있으면 weeks.json에 포함 (탭 사라짐 방지) ──
    processed_ids = {w['id'] for w in weeks_list}
    for w in weeks:
        if w['id'] in processed_ids:
            continue
        if os.path.exists(f"{OUTPUT_DIR}/{w['id']}.json"):
            weeks_list.append({'id': w['id'], 'label': w['label'], 'ref_date': w['end']})
            print(f"  ♻️  {w['id']} 이번 실행 실패/누락 → 기존 파일로 탭 유지")

    # 최신→과거 순서 정렬 (weeks 순서 기준)
    _order = {w['id']: idx for idx, w in enumerate(weeks)}
    weeks_list.sort(key=lambda x: _order.get(x['id'], 999))

    # weeks.json 저장
    with open(f"{OUTPUT_DIR}/weeks.json", 'w', encoding='utf-8') as f:
        json.dump(weeks_list, f, ensure_ascii=False)
    print(f"\n✅ weeks.json 저장 완료 ({len(weeks_list)}개 주차)")

    # ── 만료 임박 썸네일 링크 갱신 (이미지 저장 없이 URL만 교체) ──
    try:
        refresh_expiring_thumbs(token, OUTPUT_DIR)
    except Exception as e:
        print(f"  ⚠️  썸네일 갱신 패스 오류(스킵): {e}")

    print(THUMB_PATCH_NOTE)
    print("\n🎉 동기화 완료! data/ 폴더를 GitHub에 push하세요.")
    print("   git add data/ && git commit -m 'Auto sync' && git push")


if __name__ == '__main__':
    main()
