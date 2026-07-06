"""환경변수에서 카페24 인증 정보를 읽어옵니다."""
import os

from dotenv import load_dotenv

load_dotenv()

MALL_ID = os.getenv("CAFE24_MALL_ID", "")
CLIENT_ID = os.getenv("CAFE24_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CAFE24_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("CAFE24_REDIRECT_URI", "")

# 매출 대시보드에 필요한 최소 권한
SCOPES = "mall.read_order,mall.read_product,mall.read_store"

# 카페24 API 버전(날짜 형식). 최신 버전은 개발자센터 문서에서 확인하세요.
API_VERSION = "2026-03-01"

TOKEN_FILE = "token.json"
API_BASE = f"https://{MALL_ID}.cafe24api.com"
