"""카페24 판매 데이터 → 메타 리포트와 동일한 주차 구조로 집계.

meta_sync.py 와 같은 주차 규격(월~일, KST 기준, id=2026-W25, 라벨=6/15~21)으로
'일자 × 제품 × 색상(옵션)별 판매수량·매출'을 집계해 data/sales.json 을 만든다.
정적 대시보드(index.html)가 메타 주차 JSON과 이 파일을 주차 id로 붙여 비교한다.

⚠️ 카페24 주문 응답의 필드명은 쇼핑몰 설정·API 버전에 따라 다르다.
   이 스크립트는 여러 후보 키를 시도하고, 실행 시 실제 필드명을 로그로 출력한다.
   로그를 보고 아래 *_KEYS 상수만 맞추면 된다. (원본 주문/개인정보는 저장하지 않음)
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from cafe24_client import Cafe24Client

# ────────────────────────── 설정 ──────────────────────────
OUTPUT_DIR   = "./data"                    # meta_sync.py 와 동일
SALES_FILE   = f"{OUTPUT_DIR}/sales.json"
START_SUNDAY = datetime(2026, 5, 10)       # meta_sync.py 와 동일 (2026-W19)
KST          = timezone(timedelta(hours=9))

# 주문에서 '주문일자'로 쓸 필드 후보 (앞에서부터 존재하는 것 사용)
DATE_KEYS    = ["order_date", "payment_date", "ordered_date", "created_date"]

# 집계 대상 라인: 옵션명에 '오딧'이 있는 항목만, 아래 규칙으로 5개 라인 분류.
#  - '플랩'이 있으면 인치와 무관하게 '플랩'
#  - 없으면 인치(29/26/24/20)로 분류
# (상품번호는 프로모션마다 바뀌므로 옵션명으로 판별)
LINE_TRIGGER = "오딧"                       # 이 단어가 있어야 집계 대상
INCH_LINES   = ["29인치", "26인치", "24인치", "20인치"]
# 집계에서 제외할 부속품 (본품 캐리어가 아님). 공백 무시로 매칭.
EXCLUDE_KEYWORDS = ["패커블", "커버"]

# 색상 추출용 키워드 사전 (긴 단어 우선 매칭). 필요 시 자유롭게 추가하세요.
COLOR_KEYWORDS = [
    "화이트", "실버", "다크그레이", "블랙",
    "솔티블루", "펄스레드", "아이시핑크", "웻그린",
]
COLOR_KEYWORDS.sort(key=len, reverse=True)  # '다크그레이'가 '그레이'보다 먼저 매칭되도록

# 리포트 표시 순서 (판매 0이어도 이 순서/구성으로 항상 노출)
ALL_LINES  = ["플랩", "29인치", "26인치", "24인치", "20인치"]
ALL_COLORS = ["화이트", "실버", "다크그레이", "블랙",
              "솔티블루", "펄스레드", "아이시핑크", "웻그린"]


# ────────────────────────── 유틸 ──────────────────────────


def to_int(val):
    try:
        return int(float(str(val).replace(",", "").strip() or 0))
    except (ValueError, TypeError):
        return 0


def pick(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def order_date_kst(order):
    """주문일자를 KST 기준 'YYYY-MM-DD' 로 반환 (카페24 order_date는 이미 KST)."""
    raw = pick(order, DATE_KEYS)
    if not raw:
        return None
    s = str(raw)
    # '2026-06-20T14:33:00+09:00' 또는 '2026-06-20' 모두 앞 10자리가 KST 날짜
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def get_items(order):
    """embed=items 로 받은 주문 항목 리스트를 반환 (구조 방어)."""
    items = order.get("items")
    if isinstance(items, list):
        return items
    if isinstance(items, dict):  # 드물게 dict로 감싸지는 경우
        for v in items.values():
            if isinstance(v, list):
                return v
    return []


def option_string(item):
    """옵션 텍스트를 문자열로 정규화 (색상 추출용)."""
    for k in ("option_value", "option_text"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    opts = item.get("options")
    if isinstance(opts, list):
        parts = []
        for o in opts:
            if isinstance(o, dict):
                parts.append(str(pick(o, ["option_value", "option_text", "value"], "")))
            else:
                parts.append(str(o))
        return " ".join(p for p in parts if p).strip()
    if isinstance(opts, str):
        return opts.strip()
    return ""


def _nospace(s):
    """공백 제거 후 비교용 문자열 (예: '아이시 핑크' → '아이시핑크')."""
    return re.sub(r"\s+", "", s or "")


def extract_color(opt_str):
    """옵션 문자열에서 지정 색상 키워드를 추출 (공백 무시, 없으면 '미상')."""
    o = _nospace(opt_str)
    if not o:
        return "미상"
    for kw in COLOR_KEYWORDS:
        if _nospace(kw) in o:      # '아이시 핑크' == '아이시핑크'
            return kw
    return "미상"  # 8개 색상에 없으면 미상 (로그에서 확인 후 필요시 추가)


# ────────────────────────── 주차 ──────────────────────────
def build_weeks(today, include_live=True):
    """주차 목록 (최신→과거). include_live면 이번 주(진행 중)를 RT-LIVE로 맨 앞에 추가.
    완료 주차는 meta_sync.py 와 동일 규격(월~일, id=2026-W25)."""
    completed = []
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)
    cur = last_sunday
    while cur >= START_SUNDAY:
        start = cur - timedelta(days=6)
        if cur.date() < today.date():          # 완료된 주차만
            completed.append({
                "id":    cur.strftime("%Y-W%U"),
                "label": (f"{start.month}/{start.day}~{cur.day}" if start.month==cur.month
                          else f"{start.month}/{start.day}~{cur.month}/{cur.day}"),
                "start": start.strftime("%Y-%m-%d"),
                "end":   cur.strftime("%Y-%m-%d"),
            })
        cur -= timedelta(weeks=1)

    if include_live:
        live_start = today - timedelta(days=today.weekday())   # 이번 주 월요일
        live = {
            "id":    "RT-LIVE",                                 # 대시보드 실시간 탭과 동일 키
            "label": f"{live_start.month}/{live_start.day}~{today.month}/{today.day}",
            "start": live_start.strftime("%Y-%m-%d"),
            "end":   today.strftime("%Y-%m-%d"),
        }
        return [live] + completed
    return completed


def date_to_week_id(date_str, weeks):
    """'YYYY-MM-DD' 가 속한 주차 id 반환 (없으면 None = 이번 주 진행분 등)."""
    for w in weeks:
        if w["start"] <= date_str <= w["end"]:
            return w["id"]
    return None


# ────────────────────────── 디버그 ──────────────────────────
def log_field_sample(orders):
    """실제 필드명을 Actions 로그에 출력 (개인정보는 저장하지 않음)."""
    if not orders:
        print("⚠️ 주문이 없어 필드 샘플을 출력할 수 없습니다.")
        return
    o = orders[0]
    print("\n────── 필드명 점검 (첫 주문) ──────")
    print("주문 최상위 키:", sorted(o.keys()))
    print("사용된 주문일자 필드:",
          next((k for k in DATE_KEYS if o.get(k)), "❌ 못 찾음"),
          "→", order_date_kst(o))
    items = get_items(o)
    if items:
        it = items[0]
        opt = option_string(it)
        print("항목(item) 키 개수:", len(it.keys()))
        print("  product_name:", it.get("product_name"))
        print("  옵션 문자열 :", opt)
        print("  → 라인 매칭 :", matched_line(opt), "| 색상:", extract_color(opt))
        print("  quantity    :", it.get("quantity"),
              "| claim_quantity:", it.get("claim_quantity"))
    else:
        print("⚠️ items 가 비어있습니다. get_orders 의 embed=items 설정을 확인하세요.")
    print("──────────────────────────────────\n")


# ────────────────────────── 집계 ──────────────────────────
def matched_line(opt_str):
    """옵션 문자열을 5개 라인 중 하나로 분류 (공백 무시).
    '오딧'이 없으면 None. 패커블 커버 등 부속품은 제외. '플랩' 있으면 무조건 '플랩', 없으면 인치."""
    o = _nospace(opt_str)
    if _nospace(LINE_TRIGGER) not in o:
        return None
    if any(_nospace(kw) in o for kw in EXCLUDE_KEYWORDS):   # 패커블 커버 등 부속품 제외
        return None
    if "플랩" in o:
        return "플랩"
    for inch in INCH_LINES:
        if _nospace(inch) in o:      # '20인치'
            return inch
    return "기타"                    # 오딧이지만 인치/플랩 미표기 → 확인용


def aggregate(orders, weeks):
    """주문 → (주차×라인×색상) 및 (일자×라인×색상) 판매수량 집계.
    라인 = 옵션명 키워드(오딧 등). product_no는 프로모션마다 바뀌므로 라인으로 묶는다."""
    week_ids = {w["id"] for w in weeks}

    wk = defaultdict(lambda: {"qty": 0, "cancel_qty": 0})   # (week_id, line, color)
    dl = defaultdict(lambda: {"qty": 0, "cancel_qty": 0})   # (date, line, color)
    color_unknown = defaultdict(int)                        # 미상 색상 옵션 샘플 카운트
    etc_line      = defaultdict(int)                        # '기타' 라인 옵션 샘플
    skipped = 0

    for od in orders:
        date_str = order_date_kst(od)
        if not date_str:
            skipped += 1
            continue
        wid = date_to_week_id(date_str, weeks)   # None이면 완료 주차 밖(이번 주 등)
        for it in get_items(od):
            opt_str = option_string(it)
            line = matched_line(opt_str)
            if not line:                          # '오딧' 없으면 제외
                continue
            qty = to_int(it.get("quantity"))
            if qty <= 0:
                continue
            cancel = to_int(it.get("claim_quantity"))
            color = extract_color(opt_str)
            if color == "미상":
                color_unknown[opt_str] += 1
            if line == "기타":
                etc_line[opt_str] += 1

            dl[(date_str, line, color)]["qty"] += qty
            dl[(date_str, line, color)]["cancel_qty"] += cancel
            if wid in week_ids:
                wk[(wid, line, color)]["qty"] += qty
                wk[(wid, line, color)]["cancel_qty"] += cancel

    # ── matrix: {week_id: {line: {color: qty}}} — 5개 라인 × 8개 색상 항상 0 채움 ──
    matrix = {}
    for w in weeks:
        matrix[w["id"]] = {ln: {c: 0 for c in ALL_COLORS} for ln in ALL_LINES}
    for (wid, line, color), v in wk.items():
        if wid in matrix and line in matrix[wid] and color in matrix[wid][line]:
            matrix[wid][line][color] += v["qty"]        # 판매 수량(gross)
        # (기타 라인/미상 색상은 매트릭스에서 제외 — 로그로만 확인)

    # ── daily_line: 일자별 추이/상관용 (일자×라인×색상) ──
    daily_line = [{
        "date": d, "line": line, "color": color,
        "qty": v["qty"], "net_qty": v["qty"] - v["cancel_qty"],
    } for (d, line, color), v in sorted(dl.items())]

    return matrix, daily_line, color_unknown, etc_line, skipped


# ────────────────────────── 메인 ──────────────────────────
def main():
    today = datetime.now(KST).replace(tzinfo=None)
    weeks = build_weeks(today)
    if not weeks:
        print("❌ 완료된 주차가 없습니다.")
        sys.exit(1)

    # 수집 기간: 가장 과거 주차의 월요일 ~ 오늘(KST)
    collect_start = weeks[-1]["start"]
    collect_end   = today.strftime("%Y-%m-%d")
    print(f"📅 주차 {len(weeks)}개 | 수집 기간 {collect_start} ~ {collect_end}")

    client = Cafe24Client()
    print("🛒 주문 수집 중...")
    orders = client.get_orders(collect_start, collect_end)
    print(f"✅ 주문 {len(orders)}건 수집")

    log_field_sample(orders)   # 실제 필드명 점검 (로그 전용)

    matrix, daily_line, color_unknown, etc_line, skipped = aggregate(orders, weeks)
    if skipped:
        print(f"⚠️ 주문일자 누락으로 건너뛴 주문: {skipped}건")

    out = {
        "generated_at": datetime.now(KST).isoformat(),
        "weeks": weeks,
        "lines": ALL_LINES,
        "colors": ALL_COLORS,
        "matrix": matrix,          # {week_id: {line: {color: qty}}} (5×8, 0 채움)
        "daily_line": daily_line,  # 일자별 추이/상관용
    }
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(SALES_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"\n💾 {SALES_FILE} 저장 완료  (라인 {len(ALL_LINES)} × 색상 {len(ALL_COLORS)})")
    for w in weeks[:5]:
        m = matrix.get(w["id"], {})
        line_tot = {ln: sum(m.get(ln, {}).values()) for ln in ALL_LINES}
        wtot = sum(line_tot.values())
        parts = " ".join(f"{ln}:{line_tot[ln]}" for ln in ALL_LINES)
        print(f"   {w['label']:>10} ({w['id']}) 합계 {wtot:,}개  |  {parts}")

    # 색상 '미상'으로 빠진 옵션 샘플 (색상 사전 보강 참고용)
    if color_unknown:
        top = sorted(color_unknown.items(), key=lambda x: -x[1])[:6]
        print("\n⚠️ 색상 '미상' 옵션 샘플 (COLOR_KEYWORDS 보강 참고):")
        for opt, cnt in top:
            print(f"     ({cnt}건) {opt}")
    # 라인 '기타'(오딧이지만 인치/플랩 미표기) 샘플
    if etc_line:
        top = sorted(etc_line.items(), key=lambda x: -x[1])[:6]
        print("\n⚠️ 라인 '기타' 옵션 샘플 (인치/플랩 판별 안 됨 → 규칙 확인):")
        for opt, cnt in top:
            print(f"     ({cnt}건) {opt}")


if __name__ == "__main__":
    main()
