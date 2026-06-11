#!/usr/bin/env python3
"""
서울·경기 지역의 '현재 전시중'인 전시 목록을 두 공공 API에서 수집·병합해
data/exhibitions.json 으로 저장한다.

데이터 출처:
  1) 한국문화정보원 '한눈에보는문화정보조회서비스' (공공데이터포털 키: SERVICE_KEY)
     https://apis.data.go.kr/B553457/cultureinfo/area2   (XML)
  2) 서울 열린데이터광장 '서울시 문화행사 정보' (서울 키: SEOUL_API_KEY, 선택)
     http://openapi.seoul.go.kr:8088/{KEY}/json/culturalEventInfo/...

- 두 소스를 합친 뒤 제목을 정규화해 중복(같은 전시)을 제거한다.
- SEOUL_API_KEY 가 없으면 1번 소스만 사용한다(graceful).
- GitHub Actions에서 매일 실행되어 결과 JSON을 커밋한다.
"""

import os
import sys
import re
import json
import datetime as dt
import urllib.parse
import xml.etree.ElementTree as ET

import requests


# ---------------------------------------------------------------------------
# 설정 (필요하면 여기만 고치면 된다)
# ---------------------------------------------------------------------------
# 한국문화정보원 '한눈에보는문화정보조회서비스' (= 공연전시정보 신버전)
# 지역별(area2) 조회. 이 API의 from~to 는 '그 기간에 종료되는' 항목을 반환하므로,
# '현재 진행중'을 얻으려면 from=오늘 ~ to=오늘+N일 로 앞쪽을 조회한 뒤
# start<=오늘<=end 로 거른다.
BASE = "https://apis.data.go.kr/B553457/cultureinfo/area2"

# 지역별 조회 대상. (라벨, sido 파라미터 후보 목록) — 앞 값부터 시도해 데이터가
# 나오는 표기를 자동 선택한다. (API가 '서울' 인지 '서울특별시' 인지 불확실하므로)
REGIONS = [
    ("서울", ["서울", "서울특별시"]),
    ("경기", ["경기", "경기도"]),
]
REGION_KEYWORDS = ("서울", "경기")   # 응답 area 필드 안전 필터

# '전시'만 남기기 위한 분류 키워드. serviceName(또는 realmName)에 아래 단어가
# 포함되면 채택한다. (공연/교육·체험 등은 제외) — 비우면 전부 통과.
GENRE_KEYWORDS = ("전시", "미술", "박물")

# 오늘부터 며칠 뒤까지 종료되는 항목을 조회할지 (이 안에서 진행중인 전시를 잡는다)
WINDOW_DAYS = 180

ROWS = 100          # 페이지당 요청 행 수 (API가 더 적게 줄 수 있음 — totalCount로 판단)
MAX_PAGES = 250     # 안전장치 (10건/페이지 가정 시 지역당 최대 2,500건)
TIMEOUT = 20

# --- 서울 열린데이터광장 '서울시 문화행사 정보' (선택 보강 소스) ---
SEOUL_BASE = "http://openapi.seoul.go.kr:8088"
SEOUL_SERVICE = "culturalEventInfo"
SEOUL_CHUNK = 1000      # 서울 API는 한 번에 최대 1000건
SEOUL_MAX_CHUNKS = 10   # 최대 10,000건까지
# CODENAME(분류)에 이 단어가 들어가면 전시로 본다.
SEOUL_GENRE_KEYWORDS = ("전시", "미술")

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
    """'20260401', '2026-04-01', '2026-04-01 00:00:00.0' → '2026-04-01'.
    실패 시 원문 반환."""
    s = (s or "").strip()
    m = re.search(r"(\d{4})\D?(\d{2})\D?(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return s


def norm_key(s):
    """제목을 중복 판정용 키로 정규화: 소문자화 + 공백·기호 제거.
    (Python 3 정규식의 \\w 는 한글을 포함하므로 \\W 로 기호/공백만 제거)"""
    s = (s or "").lower()
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE)


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


def fetch_page(service_key, page, date_from, date_to, sido=None):
    params = {
        "from": date_from,
        "to": date_to,
        "rows": ROWS,
        "cPage": page,
        "sortStdr": 1,
    }
    if sido:
        params["sido"] = sido
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
        "price": "",            # 이 API는 가격 정보를 주지 않음
        "source": "문화포털",
    }


def keep(rec, today):
    if not rec["title"]:
        return False
    # 지역 필터: area2 로 이미 지역 조회를 했으므로, area 값이 채워져 있고
    # 그게 서울·경기가 아닐 때만 제외한다(빈 값이면 신뢰하고 통과).
    area = rec["area"] or ""
    if area and REGION_KEYWORDS and not any(k in area for k in REGION_KEYWORDS):
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


def collect_region(service_key, label, sido_candidates, date_from, date_to, today, seen):
    """한 지역(서울/경기)을 sido 표기 후보를 바꿔가며 조회해 seen 에 채운다."""
    # 데이터가 나오는 sido 표기를 찾는다 (page 1 으로 탐색)
    chosen = None
    first_nodes = None
    first_total = None
    for cand in sido_candidates:
        try:
            xml_text = fetch_page(service_key, 1, date_from, date_to, sido=cand)
            nodes, total = parse_items(xml_text)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{label}/{cand}] 오류: {exc}", file=sys.stderr)
            continue
        if nodes:
            chosen, first_nodes, first_total = cand, nodes, total
            print(f"  [{label}] sido='{cand}' 사용 (전체 {total}건)")
            break
        else:
            print(f"  [{label}] sido='{cand}' → 0건, 다음 표기 시도")
    if not chosen:
        print(f"  [{label}] 데이터를 찾지 못함")
        return

    kept0 = len(seen)
    fetched = 0
    total_count = first_total
    page = 1
    nodes = first_nodes
    while True:
        kb = len(seen)
        for n in nodes:
            rec = to_record(n)
            if keep(rec, today):
                rec["area"] = short_area(rec["area"])
                seen[rec["id"]] = rec
        fetched += len(nodes)
        print(f"    {label} page {page}: 수신 {len(nodes)} (누적 {fetched}), "
              f"채택 +{len(seen)-kb} (지역누적 {len(seen)-kept0})")
        if total_count is not None and fetched >= total_count:
            break
        if page >= MAX_PAGES:
            print(f"    {label}: MAX_PAGES({MAX_PAGES}) 도달, 중단")
            break
        page += 1
        try:
            xml_text = fetch_page(service_key, page, date_from, date_to, sido=chosen)
            nodes, total = parse_items(xml_text)
        except Exception as exc:  # noqa: BLE001
            print(f"    {label} page {page} 오류: {exc}", file=sys.stderr)
            break
        if not nodes:
            break


def seoul_to_record(row, today):
    """서울 culturalEventInfo 의 한 행(dict)을 공통 레코드로 변환."""
    title = (row.get("TITLE") or "").strip()
    codename = (row.get("CODENAME") or "").strip()
    is_free = (row.get("IS_FREE") or "").strip()        # '무료' / '유료'
    fee = (row.get("USE_FEE") or "").strip()
    price = is_free or ("무료" if "무료" in fee else "")
    return {
        "id": "seoul:" + (title or row.get("ORG_LINK", "")),
        "title": title,
        "place": (row.get("PLACE") or "").strip(),
        "area": "서울",
        "sigungu": (row.get("GUNAME") or "").strip(),
        "genre": codename,
        "startDate": norm_date(row.get("STRTDATE")),
        "endDate": norm_date(row.get("END_DATE")),
        "thumbnail": (row.get("MAIN_IMG") or "").strip(),
        "gpsX": (row.get("LOT") or "").strip(),   # 경도(X)
        "gpsY": (row.get("LAT") or "").strip(),   # 위도(Y)
        "url": (row.get("ORG_LINK") or row.get("HMPG_ADDR") or "").strip(),
        "price": price,
        "source": "서울",
    }


def seoul_keep(rec, today):
    if not rec["title"]:
        return False
    # 전시 분류만 (CODENAME 에 전시/미술 포함)
    if SEOUL_GENRE_KEYWORDS and not any(k in (rec["genre"] or "") for k in SEOUL_GENRE_KEYWORDS):
        return False
    # 현재 진행중
    try:
        if rec["startDate"]:
            if dt.date.fromisoformat(rec["startDate"]) > today:
                return False
        if rec["endDate"]:
            if dt.date.fromisoformat(rec["endDate"]) < today:
                return False
    except ValueError:
        pass
    return True


def collect_seoul(seoul_key, today):
    """서울 문화행사 정보를 수집해 전시·진행중 레코드 리스트를 반환."""
    out = []
    total = None
    for chunk in range(SEOUL_MAX_CHUNKS):
        start = chunk * SEOUL_CHUNK + 1
        end = start + SEOUL_CHUNK - 1
        url = (f"{SEOUL_BASE}/{urllib.parse.quote(seoul_key, safe='')}"
               f"/json/{SEOUL_SERVICE}/{start}/{end}/")
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"  [서울] chunk {start}-{end} 오류: {exc}", file=sys.stderr)
            break

        block = data.get(SEOUL_SERVICE, {})
        result = block.get("RESULT", {})
        code = result.get("CODE", "")
        if code and not code.startswith("INFO-000"):
            print(f"  [서울] 응답: {code} {result.get('MESSAGE','')}")
            if "INFO-200" in code:   # 데이터 없음
                break
        if total is None:
            total = block.get("list_total_count")
            print(f"  [서울] 전체 {total}건")
        rows = block.get("row", []) or []
        if not rows:
            break
        kept = 0
        for row in rows:
            rec = seoul_to_record(row, today)
            if seoul_keep(rec, today):
                out.append(rec)
                kept += 1
        print(f"    서울 {start}-{end}: 수신 {len(rows)}, 전시·진행중 채택 +{kept}")
        if total is not None and end >= int(total or 0):
            break
        if len(rows) < SEOUL_CHUNK:
            break
    return out


def merge_dedup(culture_items, seoul_items):
    """두 소스를 합치고 제목 정규화 키로 중복 제거.
    먼저 들어온 레코드를 유지하되, 비어있는 필드는 중복 레코드에서 채운다."""
    by_key = {}
    order = []
    fill_fields = ("thumbnail", "url", "price", "sigungu", "gpsX", "gpsY", "place")
    dup = 0
    for rec in list(culture_items) + list(seoul_items):
        k = norm_key(rec["title"])
        if not k:
            continue
        if k not in by_key:
            by_key[k] = dict(rec)
            order.append(k)
        else:
            dup += 1
            base = by_key[k]
            for f in fill_fields:
                if not base.get(f) and rec.get(f):
                    base[f] = rec[f]
            # 출처 표시 합치기
            if rec.get("source") and rec["source"] not in base.get("source", ""):
                base["source"] = f"{base.get('source','')}+{rec['source']}"
    print(f"  병합: 문화포털 {len(culture_items)} + 서울 {len(seoul_items)} "
          f"→ 중복 {dup}건 제거 → 최종 {len(order)}건")
    return [by_key[k] for k in order]


def main():
    service_key = os.environ.get("SERVICE_KEY", "").strip()
    seoul_key = os.environ.get("SEOUL_API_KEY", "").strip()
    if not service_key and not seoul_key:
        print("SERVICE_KEY/SEOUL_API_KEY 둘 다 없습니다. 샘플 데이터 유지하고 종료.",
              file=sys.stderr)
        sys.exit(0)

    today = dt.datetime.now(KST).date()
    date_from = today.strftime("%Y%m%d")
    date_to = (today + dt.timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")

    # 1) 문화포털 (서울·경기)
    culture_seen = {}
    if service_key:
        print(f"[문화포털] 조회 기간(종료일 기준): {date_from} ~ {date_to} (오늘={today})")
        for label, cands in REGIONS:
            collect_region(service_key, label, cands, date_from, date_to, today, culture_seen)
    else:
        print("[문화포털] SERVICE_KEY 없음 — 건너뜀")
    culture_items = list(culture_seen.values())

    # 2) 서울 열린데이터 (선택)
    seoul_items = []
    if seoul_key:
        print("[서울] 문화행사 정보 수집")
        seoul_items = collect_seoul(seoul_key, today)
    else:
        print("[서울] SEOUL_API_KEY 없음 — 건너뜀(문화포털만 사용)")

    # 3) 병합 + 중복 제거
    items = merge_dedup(culture_items, seoul_items)

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
