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
# 주문항목(line item)에서 '결제금액(라인 합계)'으로 쓸 필드 후보
LINE_AMOUNT_KEYS = ["actual_payment_amount", "payment_amount",
                    "product_price_amount", "order_price_amount"]

# 색상 추출용 키워드 사전 (긴 단어 우선 매칭). 필요 시 자유롭게 추가하세요.
COLOR_KEYWORDS = [
    "다크그레이", "라이트그레이", "차콜그레이", "펄스레드", "와인레드",
    "네이비", "베이지", "브라운", "카키", "블랙", "화이트", "그레이",
    "핑크", "레드", "블루", "그린", "옐로우", "퍼플", "아이보리",
    "크림", "라벤더", "민트", "코랄", "오렌지", "실버", "골드",
]
COLOR_KEYWORDS.sort(key=len, reverse=True)  # '다크그레이'가 '그레이'보다 먼저 매칭되도록


# ────────────────────────── 유틸 ──────────────────────────
def to_amount(val):
    """숫자/문자/딕셔너리에서 금액을 float으로 안전 변환."""
    if isinstance(val, bool) or val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.replace(",", "").strip() or 0)
        except ValueError:
            return 0.0
    if isinstance(val, dict):
        for k in LINE_AMOUNT_KEYS + ["amount", "price"]:
            if k in val:
                return to_amount(val[k])
    return 0.0


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


def extract_color(opt_str):
    """옵션 문자열에서 색상 키워드를 추출 (없으면 원본 옵션 문자열 유지)."""
    if not opt_str:
        return "미상"
    for kw in COLOR_KEYWORDS:
        if kw in opt_str:
            return kw
    return opt_str  # 사전에 없으면 원본 유지 → 로그 보고 키워드 추가


def line_revenue(item, qty):
    """항목 결제금액(라인 합계). 명시 필드가 없으면 단가×수량으로 추정."""
    amt = pick(item, LINE_AMOUNT_KEYS)
    if amt is not None:
        return to_amount(amt)
    unit = to_amount(item.get("product_price")) + to_amount(item.get("option_price"))
    return unit * qty


# ────────────────────────── 주차 ──────────────────────────
def build_weeks(today):
    """meta_sync.py 와 동일한 주차 목록 (최신→과거)."""
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)
    weeks = []
    cur = last_sunday
    while cur >= START_SUNDAY:
        start = cur - timedelta(days=6)
        if cur.date() < today.date():          # 완료된 주차만
            weeks.append({
                "id":    cur.strftime("%Y-W%U"),
                "label": f"{cur.month}/{start.day}~{cur.day}",
                "start": start.strftime("%Y-%m-%d"),
                "end":   cur.strftime("%Y-%m-%d"),
            })
        cur -= timedelta(weeks=1)
    return weeks


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
        print("항목(item) 키:", sorted(it.keys()))
        print("  product_no  :", it.get("product_no"))
        print("  product_name:", it.get("product_name"))
        print("  variant_code:", it.get("variant_code"))
        print("  옵션 문자열 :", option_string(it), "→ 색상:", extract_color(option_string(it)))
        print("  quantity    :", it.get("quantity"),
              "| claim_quantity:", it.get("claim_quantity"))
        print("  라인금액필드:",
              next((k for k in LINE_AMOUNT_KEYS if it.get(k) is not None), "❌ 없음 → 단가×수량 추정"),
              "→", line_revenue(it, to_int(it.get("quantity"))))
    else:
        print("⚠️ items 가 비어있습니다. get_orders 의 embed=items 설정을 확인하세요.")
    print("──────────────────────────────────\n")


# ────────────────────────── 집계 ──────────────────────────
def aggregate(orders, weeks):
    """주문 → (주차×제품×색상) 및 (일자×제품×색상) 집계."""
    week_ids = {w["id"] for w in weeks}

    # (week_id, product_no, color) → 집계
    wk = defaultdict(lambda: {"qty": 0, "cancel_qty": 0, "revenue": 0.0})
    # (date, product_no, color) → 집계
    dl = defaultdict(lambda: {"qty": 0, "cancel_qty": 0, "revenue": 0.0})
    pname = {}                                   # product_no → 이름
    pcolors = defaultdict(set)                   # product_no → 색상 집합
    skipped = 0

    for od in orders:
        date_str = order_date_kst(od)
        if not date_str:
            skipped += 1
            continue
        wid = date_to_week_id(date_str, weeks)   # None이면 완료 주차 밖(이번 주 등)
        for it in get_items(od):
            pno = it.get("product_no") or it.get("product_code")
            if pno is None:
                skipped += 1
                continue
            pno = str(pno)
            qty = to_int(it.get("quantity"))
            if qty <= 0:
                continue
            cancel = to_int(it.get("claim_quantity"))
            rev = line_revenue(it, qty)
            color = extract_color(option_string(it))

            pname.setdefault(pno, it.get("product_name") or f"상품{pno}")
            pcolors[pno].add(color)

            dkey = (date_str, pno, color)
            dl[dkey]["qty"] += qty
            dl[dkey]["cancel_qty"] += cancel
            dl[dkey]["revenue"] += rev

            if wid in week_ids:
                wkey = (wid, pno, color)
                wk[wkey]["qty"] += qty
                wk[wkey]["cancel_qty"] += cancel
                wk[wkey]["revenue"] += rev

    # ── by_week_product 구성: {week_id: [{product, ..., by_color:[...]}]} ──
    # 중간 구조: week_id → product_no → {합계, colors:{color:{...}}}
    tmp = defaultdict(lambda: defaultdict(
        lambda: {"qty": 0, "cancel_qty": 0, "revenue": 0.0, "colors": defaultdict(
            lambda: {"qty": 0, "cancel_qty": 0, "revenue": 0.0})}))
    for (wid, pno, color), v in wk.items():
        p = tmp[wid][pno]
        p["qty"] += v["qty"]; p["cancel_qty"] += v["cancel_qty"]; p["revenue"] += v["revenue"]
        c = p["colors"][color]
        c["qty"] += v["qty"]; c["cancel_qty"] += v["cancel_qty"]; c["revenue"] += v["revenue"]

    by_week_product = {}
    for wid, prods in tmp.items():
        rows = []
        for pno, p in prods.items():
            by_color = [{
                "color": color, "qty": c["qty"], "net_qty": c["qty"] - c["cancel_qty"],
                "cancel_qty": c["cancel_qty"], "revenue": round(c["revenue"]),
            } for color, c in sorted(p["colors"].items(), key=lambda x: -x[1]["qty"])]
            rows.append({
                "product_no": pno, "product_name": pname.get(pno, ""),
                "qty": p["qty"], "net_qty": p["qty"] - p["cancel_qty"],
                "cancel_qty": p["cancel_qty"], "revenue": round(p["revenue"]),
                "by_color": by_color,
            })
        rows.sort(key=lambda r: -r["qty"])
        by_week_product[wid] = rows

    # ── daily_product: 상관분석/일자 추이용 (일자×제품×색상) ──
    daily_product = [{
        "date": d, "product_no": pno, "product_name": pname.get(pno, ""),
        "color": color, "qty": v["qty"], "net_qty": v["qty"] - v["cancel_qty"],
        "cancel_qty": v["cancel_qty"], "revenue": round(v["revenue"]),
    } for (d, pno, color), v in sorted(dl.items())]

    # ── products 카탈로그: 오딧/플랩 → product_no 매핑을 짤 때 참고용 ──
    products = [{
        "product_no": pno, "product_name": pname[pno],
        "colors": sorted(pcolors[pno]),
    } for pno in sorted(pname, key=lambda x: pname[x])]

    return by_week_product, daily_product, products, skipped


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

    by_week_product, daily_product, products, skipped = aggregate(orders, weeks)
    if skipped:
        print(f"⚠️ 날짜/상품번호 누락으로 건너뛴 항목: {skipped}건 (필드명 점검 필요할 수 있음)")

    out = {
        "generated_at": datetime.now(KST).isoformat(),
        "weeks": weeks,
        "products": products,
        "by_week_product": by_week_product,
        "daily_product": daily_product,
    }
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(SALES_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    tot = sum(r["qty"] for rows in by_week_product.values() for r in rows)
    print(f"\n💾 {SALES_FILE} 저장 완료")
    print(f"   제품 {len(products)}종 | 완료 주차 판매수량 합계 {tot:,}개")
    for w in weeks[:4]:
        rows = by_week_product.get(w["id"], [])
        wsum = sum(r["qty"] for rows2 in [rows] for r in rows2)
        print(f"   {w['label']:>10} ({w['id']}): 제품 {len(rows)}종 · {wsum:,}개")


if __name__ == "__main__":
    main()
