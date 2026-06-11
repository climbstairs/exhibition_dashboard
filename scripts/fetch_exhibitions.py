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

# '전시'만 남기기 위한 분류 키워드. serviceName(또는 realmName)에 아래 단어가
# 포함되면 채택한다. (공연/교육·체험 등은 제외) — 비우면 전부 통과.
GENRE_KEYWORDS = ("전시", "미술", "박물")

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
    """(items, total_count) 반환."""
    root = ET.fromstring(xml_text)
    # 신버전 헤더: <resultCode>00</resultCode> 가 정상
    code = text(root, ".//resultCode")
    if code and code != "00":
        msg = text(root, ".//resultMsg") or "unknown"
        raise RuntimeError(f"API 오류({code}): {msg}")
    # 구버전 헤더 호환
    success = root.find(".//SuccessYN")
    if success is not None and success.text and success.text.strip().upper() == "N":
        msg = text(root, ".//ErrMsg") or text(root, ".//returnAuthMsg")
        raise RuntimeError(f"API 오류 응답: {msg or 'unknown'}")

    items = root.findall(".//item")
    if not items:
        items = root.findall(".//perforList")

    total_txt = text(root, ".//totalCount")
    try:
        total = int(total_txt) if total_txt else None
    except ValueError:
        total = None
    return items, total


def to_record(node):
    seq = text(node, "seq", "localId", "id")
    title = text(node, "title")
    place = text(node, "place", "spatialCoverage")
    area = text(node, "area")
    sigungu = text(node, "sigungu", "gugun")
    service = text(node, "serviceName")          # 전시 / 공연 / 교육·체험
    realm = text(node, "realmName", "genre")      # 전시 / 연극 / 뮤지컬·오페라 ...
    start = norm_date(text(node, "startDate", "from"))
    end = norm_date(text(node, "endDate", "to"))
    thumb = text(node, "thumbnail", "imageObject")
    gpsx = text(node, "gpsX")
    gpsy = text(node, "gpsY")
    url = text(node, "url")

    return {
        "id": seq or title,
        "title": title,
        "place": place,
        "area": area,
        "sigungu": sigungu,
        # 분류는 serviceName 우선(전시/공연/교육), 없으면 realmName 사용
        "genre": service or realm,
        "startDate": start,
        "endDate": end,
        "thumbnail": thumb,
        "gpsX": gpsx,
        "gpsY": gpsy,
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
    # 검증된 방식: 오늘을 포함하는 범위로 조회하고, '오늘 진행중'은 코드에서 거른다.
    # (이 API는 from~to 기간과 겹치는 일정을 모두 반환한다.)
    date_from = (today - dt.timedelta(days=7)).strftime("%Y%m%d")
    date_to = today.strftime("%Y%m%d")
    print(f"조회 기간: {date_from} ~ {date_to} (오늘={today})")

    seen = {}
    fetched_total = 0
    total_count = None
    for page in range(1, MAX_PAGES + 1):
        try:
            xml_text = fetch_page(service_key, page, date_from, date_to)
            nodes, total = parse_items(xml_text)
        except Exception as exc:  # noqa: BLE001
            print(f"[page {page}] 오류: {exc}", file=sys.stderr)
            break
        if total_count is None and total is not None:
            total_count = total
        if not nodes:
            break

        kept_before = len(seen)
        for n in nodes:
            rec = to_record(n)
            if keep(rec, today):
                rec["area"] = short_area(rec["area"])
                seen[rec["id"]] = rec
        fetched_total += len(nodes)
        print(f"  page {page}: 수신 {len(nodes)}건 (누적 {fetched_total}), "
              f"서울·경기 전시 채택 +{len(seen) - kept_before} (누적 {len(seen)})"
              f"{f' / 전체 {total_count}건' if total_count else ''}")

        # 종료 조건: 받은 누적이 totalCount 이상이거나, 빈 페이지를 만나면 중단
        if total_count is not None and fetched_total >= total_count:
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
