#!/usr/bin/env python3
"""
서울·경기 지역의 '현재 전시중'인 전시 목록을 문화포털 공연전시정보 API에서
수집해 data/exhibitions.json 으로 저장한다.

- 데이터 출처: 한국문화정보원 공연전시정보조회서비스
  http://www.culture.go.kr/openapi/rest/publicperformancedisplays/period
- 인증: 공공데이터포털/문화포털에서 발급받은 serviceKey (환경변수 SERVICE_KEY)
- GitHub Actions에서 매일 실행되어 결과 JSON을 커밋한다.

응답이 XML이라 표준 라이브러리 xml.etree 로 파싱한다(추가 의존성: requests 만).
필드명이 기관/버전에 따라 다를 수 있어 .find 를 방어적으로 처리한다.
"""

import os
import sys
import json
import datetime as dt
import urllib.parse
import xml.etree.ElementTree as ET

import requests

# ---------------------------------------------------------------------------
# 설정 (필요하면 여기만 고치면 된다)
# ---------------------------------------------------------------------------
# 한국문화정보원 '한눈에보는문화정보조회서비스' (= 공연전시정보 신버전)
# 기간별 조회 오퍼레이션. 지역/장르는 응답을 받아 코드에서 필터링한다.
BASE = "https://apis.data.go.kr/B553457/cultureinfo/period2"

# 서울·경기만 남긴다. area 필드에 이 문자열이 들어가면 채택.
REGION_KEYWORDS = ("서울", "경기")

# '전시'만 남기기 위한 장르 키워드. realmName 에 아래 단어가 포함되면 채택.
# (공연/음악/연극 등은 제외) — 비워두면 전부 통과.
GENRE_KEYWORDS = ("미술", "전시", "박물")

ROWS = 100          # 페이지당 행 수
MAX_PAGES = 60      # 안전장치 (ROWS*MAX_PAGES 건까지 조회)
TIMEOUT = 20

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "exhibitions.json",
)

KST = dt.timezone(dt.timedelta(hours=9))


def text(node, *tags):
    """node 하위에서 tags 중 처음 발견되는 태그의 텍스트를 반환."""
    if node is None:
        return ""
    for tag in tags:
        el = node.find(tag)
        if el is not None and el.text:
            return el.text.strip()
    return ""


def norm_date(s):
    """'20260401' 또는 '2026-04-01' → '2026-04-01'. 실패 시 원문 반환."""
    s = (s or "").strip()
    digits = s.replace("-", "").replace(".", "").replace("/", "")
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return s


def build_url(base, service_key, params):
    """serviceKey 의 인코딩/디코딩 형태를 자동 판별해 URL을 만든다.
    - 이미 퍼센트 인코딩된 키(%2B 등 포함)면 그대로 사용
    - 디코딩(원문) 키면 한 번만 인코딩
    이렇게 하면 포털에서 어느 쪽 키를 복사하든 동작한다.
    """
    looks_encoded = "%" in service_key
    key = service_key if looks_encoded else urllib.parse.quote(service_key, safe="")
    query = "&".join(
        f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items()
    )
    return f"{base}?serviceKey={key}&{query}"


def fetch_page(service_key, page, date_from, date_to):
    params = {
        "from": date_from,
        "to": date_to,
        "rows": ROWS,
        "cPage": page,
        "sortStdr": 1,
    }
    url = build_url(BASE, service_key, params)
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def parse_items(xml_text):
    root = ET.fromstring(xml_text)
    # 정상 여부 확인 (헤더가 있으면)
    success = root.find(".//SuccessYN")
    if success is not None and success.text and success.text.strip().upper() == "N":
        msg = text(root, ".//ErrMsg") or text(root, ".//returnAuthMsg")
        raise RuntimeError(f"API 오류 응답: {msg or 'unknown'}")

    # item 노드는 perforList / item 등으로 올 수 있다.
    items = root.findall(".//perforList")
    if not items:
        items = root.findall(".//item")
    return items


def to_record(node):
    seq = text(node, "seq", "localId", "id")
    title = text(node, "title", "TITLE")
    place = text(node, "place", "EVENT_SITE", "spatialCoverage")
    area = text(node, "area", "AREA")
    genre = text(node, "realmName", "genre", "GENRE")
    start = norm_date(text(node, "startDate", "PERIOD_START", "from"))
    end = norm_date(text(node, "endDate", "PERIOD_END", "to"))
    thumb = text(node, "thumbnail", "imageObject", "IMAGE_OBJECT")
    price = text(node, "price", "CHARGE", "charge")
    url = text(node, "url", "URL")
    if not url and seq:
        url = f"https://www.culture.go.kr/wday/index.do"  # 상세 deep-link 미보장 시 포털

    return {
        "id": seq or title,
        "title": title,
        "place": place,
        "area": area,
        "genre": genre,
        "startDate": start,
        "endDate": end,
        "price": price,
        "thumbnail": thumb,
        "url": url,
    }


def keep(rec, today):
    if not rec["title"]:
        return False
    # 지역 필터
    if REGION_KEYWORDS and not any(k in (rec["area"] or "") for k in REGION_KEYWORDS):
        return False
    # 장르 필터
    if GENRE_KEYWORDS and rec["genre"] and not any(k in rec["genre"] for k in GENRE_KEYWORDS):
        return False
    # 현재 전시중: 시작 <= 오늘 <= 종료 (날짜 파싱 가능한 경우만 엄격 적용)
    try:
        if rec["startDate"]:
            s = dt.date.fromisoformat(rec["startDate"])
            if s > today:
                return False
        if rec["endDate"]:
            e = dt.date.fromisoformat(rec["endDate"])
            if e < today:
                return False
    except ValueError:
        pass
    return True


def short_area(area):
    a = area or ""
    if "서울" in a:
        return "서울"
    if "경기" in a:
        return "경기"
    return a


def main():
    service_key = os.environ.get("SERVICE_KEY", "").strip()
    if not service_key:
        print("SERVICE_KEY 환경변수가 없습니다. 샘플 데이터를 건드리지 않고 종료합니다.",
              file=sys.stderr)
        sys.exit(0)

    today = dt.datetime.now(KST).date()
    date_from = today.strftime("%Y%m%d")
    date_to = today.strftime("%Y%m%d")

    seen = {}
    for page in range(1, MAX_PAGES + 1):
        try:
            xml_text = fetch_page(service_key, page, date_from, date_to)
            nodes = parse_items(xml_text)
        except Exception as exc:  # noqa: BLE001
            print(f"[page {page}] 오류: {exc}", file=sys.stderr)
            break
        if not nodes:
            break
        for n in nodes:
            rec = to_record(n)
            if keep(rec, today):
                rec["area"] = short_area(rec["area"])
                seen[rec["id"]] = rec
        if len(nodes) < ROWS:
            break

    items = list(seen.values())
    # 폐막 임박 순 정렬
    def sort_key(r):
        try:
            return dt.date.fromisoformat(r["endDate"])
        except (ValueError, TypeError):
            return dt.date.max
    items.sort(key=sort_key)

    payload = {
        "generatedAt": dt.datetime.now(KST).isoformat(timespec="minutes"),
        "sample": False,
        "count": len(items),
        "items": items,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"저장 완료: {len(items)}건 → {OUT_PATH}")


if __name__ == "__main__":
    main()
