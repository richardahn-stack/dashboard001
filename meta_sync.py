"""
메타 광고 자동 동기화 스크립트
사용법: python meta_sync.py
"""

import requests, json, re, os, sys
from datetime import datetime, timedelta
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
        try:
            # creative 전체 필드 요청
            ad_data = api_get(
                ad_id,
                {'fields': 'creative{id,image_url,video_id,object_story_spec,asset_feed_spec}'},
                token
            )
            cr       = ad_data.get('creative', {})
            cr_id    = cr.get('id')
            video_id = cr.get('video_id')
            thumb_url = None
            img_type  = None

            if video_id:
                # ── 영상 소재 ──
                img_type = 'video'
                # 1) video thumbnails API (가장 안정적)
                try:
                    vt = api_get(f"{video_id}/thumbnails",
                                 {'fields': 'uri,is_preferred'}, token)
                    thumbs = vt.get('data', [])
                    pref = next((t for t in thumbs if t.get('is_preferred')), None)
                    thumb_url = (pref or (thumbs[0] if thumbs else {})).get('uri')
                except Exception:
                    pass
                # 2) object_story_spec.video_data.image_url fallback
                if not thumb_url:
                    vd = cr.get('object_story_spec', {}).get('video_data', {})
                    thumb_url = vd.get('image_url')
                # 3) asset_feed_spec fallback
                if not thumb_url:
                    afs = cr.get('asset_feed_spec', {})
                    vids = afs.get('videos', [])
                    if vids:
                        thumb_url = vids[0].get('thumbnail_url')
            else:
                # ── 이미지/배너 소재 ──
                img_type = 'img'
                # 1) image_url (영구 URL)
                thumb_url = cr.get('image_url')
                # 2) object_story_spec.link_data
                if not thumb_url:
                    ld = cr.get('object_story_spec', {}).get('link_data', {})
                    thumb_url = ld.get('image_url') or ld.get('picture')
                # 3) asset_feed_spec
                if not thumb_url:
                    afs = cr.get('asset_feed_spec', {})
                    imgs = afs.get('images', [])
                    if imgs:
                        thumb_url = imgs[0].get('url')

            creatives[ad_id] = {
                'thumbnail_url': thumb_url,
                'video_id':      video_id,
                'type':          img_type,
            }
        except Exception as e:
            creatives[ad_id] = {'thumbnail_url': None, 'video_id': None, 'type': None}

    found = sum(1 for c in creatives.values() if c.get('thumbnail_url'))
    print(f"    소재 {len(creatives)}개 수집 (썸네일 {found}개 확보)")
    return insights, creatives


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
                    'key': key, 'base_key': bk, 'ad_id': ins.get('ad_id'),
                    'objective': obj, 'pa_type': pa, 'landing': land,
                    'purpose': obj, 'mgr': mgr, 'media': media,
                    'spend': 0, 'impressions': 0, 'clicks': 0,
                    'purchases': 0, 'revenue': 0, 'roas_sum': 0, 'roas_n': 0,
                    'age_days': age,
                }
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
            result.append(r)
        return result

    rows = parse_insights(insights)
    prev_rows = parse_insights(prev_insights)
    prev_map = {r['key']: r for r in prev_rows}

    # 전주 대비 지표 추가
    conv = [r for r in rows if r['objective']=='전환' and r['spend']>=100000]
    avg_roas = sum(r['roas'] for r in conv)/len(conv) if conv else 0
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

        # 위닝 판정
        ws = None; wf = {}; wt = None
        if r['objective']=='전환' and r['spend']>=100000 and avg_roas>0:
            wf = {
                'roas': r['roas']>avg_roas, 'ctr': r['ctr']>avg_ctr,
                'cvr': r['cvr']>avg_cvr, 'cpc': r['cpc']>0 and r['cpc']<avg_cpc,
                'purchases': r['purchases']>avg_pur
            }
            ws = sum(wf.values()); wt = '전환'
        elif r['objective'] in ['트래픽','콘조'] and r['spend']>=30000:
            wf = {'ctr': r['ctr']>tc_avg_ctr}
            ws = 1 if wf['ctr'] else 0; wt = 'tc'

        # 소재 썸네일: 메타 API URL 우선, 없으면 드라이브 맵 fallback
        ad_id = r.get('ad_id')
        cr = creatives.get(ad_id, {})
        thumb_url = cr.get('thumbnail_url')
        video_id  = cr.get('video_id')
        img_type  = cr.get('type')



        final_rows.append({
            **r,
            'old_roas_pct': round(old_roas*100,0) if old_roas else None,
            'roas_delta': roas_delta,
            'prev_spend': int(old['spend']) if old.get('spend') else None,
            'prev_ctr': round(old['ctr'],2) if old.get('ctr') else None,
            'prev_cvr': round(old['cvr'],2) if old.get('cvr') else None,
            'prev_cpc': round(old['cpc'],0) if old.get('cpc') else None,
            'prev_purchases': int(old['purchases']) if old.get('purchases') else None,
            'prev_revenue': int(old['revenue']) if old.get('revenue') else None,
            'img': thumb_url,       # ← 드라이브 ID 대신 메타 썸네일 URL
            'img_type': img_type,
            'video': video_id,      # ← 메타 비디오 ID
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

    all_ads = sorted(final_rows, key=lambda x: x['spend'], reverse=True)
    conv_win = sorted([r for r in final_rows if r['win_type']=='전환' and (r['win_score'] or 0)>=3],
                      key=lambda x: x['revenue'], reverse=True)[:10]
    tc_win   = sorted([r for r in final_rows if r['win_type']=='tc' and (r['win_score'] or 0)>=1],
                      key=lambda x: x['ctr'], reverse=True)[:10]
    d7_all   = [r for r in final_rows if r['age_days'] is not None and r['age_days']<=7 and r['spend']>=30000]
    d7_win   = sorted([r for r in d7_all if r['roas_pct']>=200], key=lambda x: x['roas_pct'], reverse=True)
    d7_lose  = sorted([r for r in d7_all if r['roas_pct']<200],  key=lambda x: x['spend'], reverse=True)

    prev_total = sum(r['spend'] for r in prev_rows)
    prev_s = {
        'spend': int(prev_total),
        'revenue': int(sum(r['revenue'] for r in prev_rows)),
        'roas_pct': round(sum(r['revenue'] for r in prev_rows)/prev_total*100,1) if prev_total else 0,
        'purchases': int(sum(r['purchases'] for r in prev_rows)),
        'impressions': int(sum(r['impressions'] for r in prev_rows)),
        'clicks': int(sum(r['clicks'] for r in prev_rows)),
        'avg_ctr': round(sum(r['ctr'] for r in prev_rows)/len(prev_rows),2) if prev_rows else 0,
        'avg_cpc': 0,
        'active_ads': len(prev_insights),
    }

    return {
        'label': label, 'prev_label': prev_label, 'ref_date': end_date_str,
        'summary': summary, 'prev_summary': prev_s,
        'trends': {}, 'pie': {},
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
def main():
    print("=" * 50)
    print("  메타 광고 대시보드 자동 동기화")
    print("=" * 50)

    # 토큰 입력 (보안상 코드에 넣지 않음)
    token = input("\n시스템 사용자 액세스 토큰을 입력하세요: ").strip()
    if not token:
        print("❌ 토큰이 없습니다.")
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

    # 수집할 주차 설정 (최근 6주)
    today = datetime.today()
    # 가장 최근 일요일 기준
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)

    weeks = []
    for i in range(6):
        end   = last_sunday - timedelta(weeks=i)
        start = end - timedelta(days=6)
        label = f"{end.month}/{start.day}~{end.day}"
        week_id = end.strftime('%Y-W%V')
        weeks.append({
            'id': end.strftime('%Y-W%U'),
            'label': label,
            'end': end.strftime('%Y-%m-%d'),
            'start': start.strftime('%Y-%m-%d'),
        })

    print(f"\n📅 수집 대상 주차: {len(weeks)}개")
    for w in weeks:
        print(f"   {w['label']} ({w['start']} ~ {w['end']})")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    weeks_list = []

    for i, w in enumerate(weeks):
        print(f"\n[{i+1}/{len(weeks)}] {w['label']} 처리 중...")
        try:
            prev = weeks[i+1] if i+1 < len(weeks) else weeks[i]
            data = build_json_from_api(
                token,
                w['end'], prev['end'],
                w['label'], prev['label']
            )
            out_path = f"{OUTPUT_DIR}/{w['id']}.json"
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            print(f"  ✅ {out_path} 저장 완료 (소재 {len(data['all_ads'])}개)")
            weeks_list.append({'id': w['id'], 'label': w['label'], 'ref_date': w['end']})
        except Exception as e:
            print(f"  ❌ 오류: {e}")
            import traceback; traceback.print_exc()

    # weeks.json 저장
    with open(f"{OUTPUT_DIR}/weeks.json", 'w', encoding='utf-8') as f:
        json.dump(weeks_list, f, ensure_ascii=False)
    print(f"\n✅ weeks.json 저장 완료")
    print(THUMB_PATCH_NOTE)
    print("\n🎉 동기화 완료! data/ 폴더를 GitHub에 push하세요.")
    print("   git add data/ && git commit -m 'Auto sync' && git push")


if __name__ == '__main__':
    main()
