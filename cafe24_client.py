"""카페24 Admin API 클라이언트: 토큰 자동 갱신 + 주문 전체 수집."""
import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from config import (API_BASE, API_VERSION, CLIENT_ID, CLIENT_SECRET,
                    TOKEN_FILE)


class Cafe24Client:
    def __init__(self):
        self.token = self._load_token()

    # ---------- 토큰 관리 ----------
    def _load_token(self):
        if not os.path.exists(TOKEN_FILE):
            raise RuntimeError(
                "토큰 파일이 없습니다. 먼저 `python get_token.py`를 실행하세요."
            )
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f)

    def _save_token(self):
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(self.token, f, indent=2, ensure_ascii=False)

    def _is_expired(self):
        expires_at = self.token.get("expires_at")
        if not expires_at:
            return True
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        # 시간대 정보가 없으면 UTC로 간주 (우리는 항상 UTC로 저장함)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        # 만료 1분 전에 미리 갱신
        return datetime.now(timezone.utc) >= exp - timedelta(minutes=1)

    def _refresh(self):
        basic = base64.b64encode(
            f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
        ).decode()
        last = None
        for attempt in range(5):
            resp = requests.post(
                f"{API_BASE}/api/v2/oauth/token",
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.token["refresh_token"],
                },
                timeout=30,
            )
            if resp.status_code == 200:
                break
            # 카페24 서버 일시 오류(5xx)면 잠깐 쉬고 재시도
            if resp.status_code >= 500:
                wait = 2 * (attempt + 1)
                print(f"토큰 갱신 {resp.status_code} 오류, {wait}초 후 재시도 ({attempt+1}/5)")
                time.sleep(wait)
                last = resp
                continue
            # 4xx(만료·무효 등)는 재시도 무의미 → 즉시 노출
            resp.raise_for_status()
        else:
            # 5번 재시도 모두 실패
            if last is not None:
                last.raise_for_status()
        new_token = resp.json()
        # 카페24가 주는 expires_at은 시간대 표시 없는 KST라 신뢰하지 않고,
        # 항상 UTC 기준으로 직접 계산해 저장한다 (access token 2시간, 안전 여유 1h50m)
        exp = datetime.now(timezone.utc) + timedelta(hours=1, minutes=50)
        new_token["expires_at"] = exp.isoformat()
        self.token = new_token
        self._save_token()
        print("토큰 갱신 완료 (새 만료시각 UTC:", new_token["expires_at"], ")")

    def _headers(self):
        if self._is_expired():
            self._refresh()
        return {
            "Authorization": f"Bearer {self.token['access_token']}",
            "Content-Type": "application/json",
            "X-Cafe24-Api-Version": API_VERSION,
        }

    # ---------- 범용 GET (탐색/디버깅용) ----------
    def get_json(self, path, params=None):
        """
        임의의 API 엔드포인트를 GET 호출합니다.
        - path가 'http'로 시작하면 그 전체 URL을 그대로 호출 (다른 도메인 가능)
        - 아니면 기본 API_BASE 뒤에 붙여서 호출
        - 속도 제한(429)·일시 오류(5xx) 시 잠시 쉬고 자동 재시도
        실패 시 카페24가 보낸 원본 응답을 그대로 노출합니다.
        """
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        last = None
        for attempt in range(5):
            resp = requests.get(
                url,
                headers=self._headers(),
                params=params or {},
                timeout=60,
            )
            if resp.status_code == 200:
                return resp.json()
            # 429(속도제한) 또는 5xx(서버 일시오류)면 대기 후 재시도
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", 0)) or (1.5 * (attempt + 1))
                time.sleep(wait)
                last = resp
                continue
            # 그 외 오류는 즉시 노출
            raise RuntimeError(
                f"카페24 API 오류 {resp.status_code}\n"
                f"요청 URL: {resp.url}\n"
                f"응답 내용: {resp.text}"
            )
        raise RuntimeError(
            f"카페24 API 오류(재시도 초과) {last.status_code if last else '?'}\n"
            f"요청 URL: {last.url if last else url}\n"
            f"응답 내용: {last.text if last else ''}"
        )

    # ---------- 상품 / 재고 ----------
    def get_all_products(self):
        """전체 상품 목록 (product_no, product_name)을 페이지네이션으로 수집."""
        products = []
        offset = 0
        while True:
            data = self.get_json("/api/v2/admin/products",
                                 {"limit": 100, "offset": offset})
            chunk = data.get("products", [])
            if not chunk:
                break
            products.extend(chunk)
            if len(chunk) < 100:
                break
            offset += 100
            if offset >= 10000:
                break
            time.sleep(0.3)
        return products

    def get_variants(self, product_no):
        """특정 상품의 품목(옵션)별 재고 목록."""
        data = self.get_json(f"/api/v2/admin/products/{product_no}/variants")
        return data.get("variants", [])

    # ---------- 주문 수집 ----------
    def get_orders(self, start_date, end_date):
        """
        지정 기간의 주문을 전부 수집합니다.
        - 날짜는 30일 단위로 청크 분할 (API 기간 제한 대응)
        - 각 청크는 offset 페이지네이션 (limit 최대 500)
        - embed=items 로 주문 상품 항목까지 함께 조회
        """
        all_orders = []
        for chunk_start, chunk_end in _date_chunks(start_date, end_date, days=30):
            offset = 0
            while True:
                params = {
                    "start_date": chunk_start,
                    "end_date": chunk_end,
                    "limit": 500,
                    "offset": offset,
                    "embed": "items",
                }
                resp = requests.get(
                    f"{API_BASE}/api/v2/admin/orders",
                    headers=self._headers(),
                    params=params,
                    timeout=60,
                )
                if resp.status_code != 200:
                    # 카페24가 보낸 거절 사유를 그대로 노출 (원인 진단용)
                    raise RuntimeError(
                        f"카페24 API 오류 {resp.status_code}\n"
                        f"요청 URL: {resp.url}\n"
                        f"응답 내용: {resp.text}"
                    )
                orders = resp.json().get("orders", [])
                if not orders:
                    break
                all_orders.extend(orders)
                if len(orders) < 500:
                    break
                offset += 500
                if offset >= 10000:  # 카페24 offset 상한
                    break
                time.sleep(0.5)  # rate limit 보호
        return all_orders


def _date_chunks(start, end, days=30):
    """'YYYY-MM-DD' 기간을 days 단위 구간 리스트로 분할."""
    fmt = "%Y-%m-%d"
    s = datetime.strptime(start, fmt)
    e = datetime.strptime(end, fmt)
    chunks = []
    cur = s
    while cur <= e:
        chunk_end = min(cur + timedelta(days=days - 1), e)
        chunks.append((cur.strftime(fmt), chunk_end.strftime(fmt)))
        cur = chunk_end + timedelta(days=1)
    return chunks
