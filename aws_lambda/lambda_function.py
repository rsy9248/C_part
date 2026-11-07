"""
AWS Lambda: hc2-c-match  (핸들러: lambda_function.lambda_handler)

입력  {"category": "chest pain", "grade": 2, "lat": 37.87, "lng": 127.73}
출력  프론트 web/app/src/types/hospital.ts 의 MatchResult 형식
      { status, chiefComplaint, mappedConditions, radiusSteps, currentRadiusKm,
        hospitals[], needsCheckHospitals[] }
      (+ 참고용 추가 필드: category, categoryKnown, grade, diseaseCode, dataFetchedAt, dataStale)

status
  ok               : 기본 반경(5km)에서 수용 가능 병원을 찾음
  radius_expanded  : 반경을 넓혀서 찾음
  stabilize_first  : 해당 질환을 받을 병원이 없어, 질환 필터 없이 가까운 응급실(병상 있음)을 추천
  none             : 수용 가능 병원 없음

- Lambda는 로컬 SQLite가 유지되지 않으므로, 호출할 때 공공 API에서 병원 데이터를 받아 매칭합니다.
  (같은 컨테이너가 재사용되는 동안은 메모리 캐시를 써서 공공 API 호출을 줄입니다.)
- 외부 패키지 없이 표준 라이브러리만 사용합니다. (requests 등 설치 불필요)
- 질환 코드 매핑은 같은 zip 안의 rules/disease_rules.json을 사용합니다.

환경변수
  PUBLIC_API_KEY      (필수) 공공데이터포털 일반 인증키(Decoding)
  ER_REGION           (선택) 특정 시도만 수집 (예: 강원특별자치도). 비우면 전체
  BEDS_TTL_SEC        (선택) 병상/수용정보 캐시 유지 시간, 기본 300
  MAX_RESULTS         (선택) hospitals 최대 개수, 기본 20
  MAX_NEEDS_CHECK     (선택) needsCheckHospitals 최대 개수, 기본 10
  AMBULANCE_SPEED_KMH (선택) 이송 시간(etaMin) 추정용 평균 속도, 기본 40
  ROAD_FACTOR         (선택) 직선거리 -> 도로거리 보정 배수, 기본 1.3
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from math import atan2, ceil, cos, radians, sin, sqrt

from rules.disease_rules import DISEASE_RULES, UNKNOWN_CODE, classify_category, disease_api_keys

# ----------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------
SERVICE_KEY = os.environ.get("PUBLIC_API_KEY", "")
BASE_URL = "https://apis.data.go.kr/B552657/ErmctInfoInqireService"

OP_BEDS = "getEmrrmRltmUsefulSckbdInfoInqire"      # 응급실 실시간 가용병상정보
OP_SEVERE = "getSrsillDissAceptncPosblInfoInqire"  # 중증질환자 수용가능정보
OP_LIST = "getEgytListInfoInqire"                  # 응급의료기관 목록정보 (주소/전화/좌표/종별)

REGION = os.environ.get("ER_REGION", "")
REGION_PARAM = {OP_BEDS: "STAGE1", OP_SEVERE: "STAGE1", OP_LIST: "Q0"}

ROWS_PER_PAGE = int(os.environ.get("ROWS_PER_PAGE", "100"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "10"))
TIMEOUT = int(os.environ.get("API_TIMEOUT_SEC", "8"))
BEDS_TTL_SEC = int(os.environ.get("BEDS_TTL_SEC", "300"))      # 병상/수용정보: 자주 바뀜
LOCATION_TTL_SEC = 24 * 60 * 60                                 # 좌표/주소/종별: 거의 안 바뀜
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "20"))
MAX_NEEDS_CHECK = int(os.environ.get("MAX_NEEDS_CHECK", "10"))
AMBULANCE_SPEED_KMH = float(os.environ.get("AMBULANCE_SPEED_KMH", "40"))
ROAD_FACTOR = float(os.environ.get("ROAD_FACTOR", "1.3"))

RADIUS_STAGES_KM = [5, 15, 30, 50]
LEVELS = ("권역응급의료센터", "지역응급의료센터", "지역응급의료기관")   # 프론트 EmergencyLevel


class DataUnavailable(Exception):
    """공공 API에서 병원 데이터를 가져오지 못한 경우."""


# ----------------------------------------------------------------------
# 공공 API 호출 / 파싱 (표준 라이브러리만 사용)
# ----------------------------------------------------------------------
def _parse_response(text):
    """XML 또는 JSON 응답을 (items, totalCount)로 변환. 에러 응답이면 DataUnavailable."""
    text = text.strip()
    try:
        if text.startswith("<"):
            root = ET.fromstring(text)
            code = root.findtext(".//resultCode")
            if code is None:
                code = root.findtext(".//returnReasonCode")
            if code not in ("00", "0"):
                msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg")
                raise DataUnavailable(f"API 에러 코드={code}, 메시지={msg}")
            items = [
                {child.tag: (child.text or "").strip() for child in item}
                for item in root.findall(".//item")
            ]
            total = int(root.findtext(".//totalCount") or len(items))
            return items, total

        data = json.loads(text)
    except (ET.ParseError, ValueError) as e:
        raise DataUnavailable(f"응답 파싱 실패: {e}")

    response = data.get("response", {})
    header = response.get("header", {})
    code = str(header.get("resultCode", ""))
    if code not in ("00", "0"):
        raise DataUnavailable(f"API 에러 코드={code}, 메시지={header.get('resultMsg')}")
    body = response.get("body", {})
    raw = body.get("items", {})
    raw = raw.get("item", []) if isinstance(raw, dict) else []
    items = [raw] if isinstance(raw, dict) else raw
    return items, int(body.get("totalCount", len(items)))


def _fetch_operation(operation):
    """한 오퍼레이션의 모든 페이지를 받아 item 리스트로 반환. 실패하면 DataUnavailable."""
    collected = []
    for page in range(1, MAX_PAGES + 1):
        params = {
            "serviceKey": SERVICE_KEY,
            "pageNo": str(page),
            "numOfRows": str(ROWS_PER_PAGE),
        }
        if REGION and operation in REGION_PARAM:
            params[REGION_PARAM[operation]] = REGION

        url = f"{BASE_URL}/{operation}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
                text = resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as e:   # HTTPError, 타임아웃 포함
            raise DataUnavailable(f"{operation} 통신 에러: {e}")

        items, total = _parse_response(text)
        if not items:
            break
        collected.extend(items)
        if len(collected) >= total:
            break
    return collected


# 같은 Lambda 컨테이너가 재사용되는 동안 유지되는 메모리 캐시: {이름: (받은 시각, 데이터)}
_cache = {}


def _get_cached(name, operation, ttl):
    """캐시가 신선하면 재사용, 아니면 새로 받기. 새로 받기 실패 시 오래된 캐시라도 사용."""
    now = time.time()
    entry = _cache.get(name)
    if entry and now - entry[0] < ttl:
        return entry[1], False, entry[0]
    try:
        data = _fetch_operation(operation)
        if not data:
            raise DataUnavailable(f"{operation} 결과가 비어 있습니다.")
        _cache[name] = (now, data)
        return data, False, now
    except DataUnavailable as e:
        if entry:
            print(f"[hc2-c-match] {name} 갱신 실패, 이전 캐시 사용: {e}")
            return entry[1], True, entry[0]
        raise


# ----------------------------------------------------------------------
# 값 변환 / 병원 데이터 병합
# ----------------------------------------------------------------------
def _get(item, *keys, default=""):
    """대소문자 구분 없이 여러 후보 키 중 먼저 있는 값을 반환."""
    lowered = {k.lower(): v for k, v in item.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return default


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _any_yes(item, keys):
    return "Y" if any(str(_get(item, k)).strip().upper() == "Y" for k in keys) else "N"


def _yn(item, key):
    """Y/N 필드. 값이 없으면 '?'(알 수 없음)."""
    value = str(_get(item, key)).strip().upper()
    return value if value else "?"


def _optional_int(value):
    """값이 비어 있으면 None(수치 없음), 있으면 정수."""
    return None if value in ("", None) else _to_int(value)


def _index_by_hpid(items):
    return {_get(i, "hpid"): i for i in (items or []) if _get(i, "hpid")}


def _level(raw):
    """응급의료기관 종별. 프론트의 3가지 값에 해당하면 그 값으로, 아니면 원문 그대로."""
    raw = (raw or "").strip()
    for level in LEVELS:
        if level in raw:
            return level
    return raw


def _region_from_address(address):
    """주소에서 소재지(시·군·구) 추출. 예: '강원특별자치도 춘천시 ...' -> '춘천시'"""
    parts = (address or "").split()
    if not parts:
        return ""
    if parts[0].startswith("세종"):
        return parts[0]
    return parts[1] if len(parts) >= 2 else parts[0]


def load_hospitals():
    """공공 API 3종을 병렬로 받아 병원 리스트로 병합. (hospitals, {"fetched_at", "stale"}) 반환."""
    if not SERVICE_KEY:
        raise DataUnavailable("PUBLIC_API_KEY 환경변수가 설정되지 않았습니다.")

    sources = {
        "beds": (OP_BEDS, BEDS_TTL_SEC),
        "severe": (OP_SEVERE, BEDS_TTL_SEC),
        "loc": (OP_LIST, LOCATION_TTL_SEC),
    }
    with ThreadPoolExecutor(max_workers=len(sources)) as ex:
        futures = {name: ex.submit(_get_cached, name, op, ttl) for name, (op, ttl) in sources.items()}
        results = {name: f.result() for name, f in futures.items()}   # 실패 시 DataUnavailable 전파

    beds, severe, loc = (results[n][0] for n in ("beds", "severe", "loc"))
    meta = {
        "fetched_at": int(min(results["beds"][2], results["severe"][2])),
        "stale": any(results[n][1] for n in results),
    }

    api_keys = disease_api_keys()
    severe_by_id = _index_by_hpid(severe)
    loc_by_id = _index_by_hpid(loc)

    hospitals = []
    for item in beds:
        hpid = _get(item, "hpid")
        if not hpid:
            continue
        sev = severe_by_id.get(hpid, {})
        place = loc_by_id.get(hpid, {})
        address = _get(place, "dutyAddr")
        status = {code: _any_yes(sev, keys) for code, keys in api_keys.items()}

        hospitals.append({
            "hospital_id": hpid,
            "hospital_name": _get(item, "dutyName") or _get(place, "dutyName", default="알 수 없는 병원"),
            "level": _level(_get(place, "dutyEmclsName")),
            "region": _region_from_address(address),
            "lat": _to_float(_get(place, "wgs84Lat", "latitude", "lat")),
            "lng": _to_float(_get(place, "wgs84Lon", "longitude", "lon", "lng")),
            "emergency_beds": _to_int(_get(item, "hvec")),
            "operating_rooms": _optional_int(_get(item, "hvoc")),
            "ward_beds": _optional_int(_get(item, "hvgc")),
            "equipment": {
                "ct": _yn(item, "hvctayn"),
                "mri": _yn(item, "hvmriayn"),
                "angio": _yn(item, "hvangioayn"),
                "ventilator": _yn(item, "hvventiayn"),
            },
            "disease_status": status,
            # 이 병원이 수용 가능한 중증질환명 (화면 accepts)
            "accepts": [
                cond
                for code, rule in DISEASE_RULES.items()
                if status.get(code) == "Y"
                for cond in rule["conditions"]
            ],
            "phone": _get(item, "dutyTel3") or _get(place, "dutyTel3") or _get(place, "dutyTel1"),
            "updated_at": _get(item, "hvidate"),
        })
    return hospitals, meta


# ----------------------------------------------------------------------
# 매칭 결과를 프론트 Hospital 형식으로 변환
# ----------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _eta_min(distance_km):
    """직선거리로 추정한 이송 시간(분). 실제 도로/교통 상황은 반영하지 않은 대략값."""
    return max(1, ceil(distance_km * ROAD_FACTOR / AMBULANCE_SPEED_KMH * 60))


def _beds_info(h):
    """프론트 BedInfo[]: 수치가 있으면 count, 가능/불가만 알면 count=None + available."""
    beds = [{"label": "응급실", "count": max(h["emergency_beds"], 0)}]   # 음수(초과 수용)는 0으로 표시
    if h["operating_rooms"] is not None:
        beds.append({"label": "수술실", "count": max(h["operating_rooms"], 0)})
    if h["ward_beds"] is not None:
        beds.append({"label": "입원실", "count": max(h["ward_beds"], 0)})
    for label, key in (("CT", "ct"), ("MRI", "mri"), ("혈관조영", "angio"), ("인공호흡기", "ventilator")):
        value = h["equipment"].get(key)
        if value in ("Y", "N"):
            beds.append({"label": label, "count": None, "available": value == "Y"})
    return beds


def _hospital_view(h, distance_km, needs_check):
    return {
        "id": h["hospital_id"],
        "name": h["hospital_name"],
        "level": h["level"],
        "region": h["region"],
        "distanceKm": round(distance_km, 2),
        "etaMin": _eta_min(distance_km),
        "lat": h["lat"],
        "lng": h["lng"],
        "beds": _beds_info(h),
        "accepts": h["accepts"],
        "phone": h["phone"],
        "needsCheck": needs_check,
    }


def evaluate(hospitals, lat, lng, radius, disease_code):
    """
    반경 내에서 질환을 받을 수 있는 병원을 거리순으로 나눈다.
      ok    : 응급실 병상 > 0  -> hospitals
      needs : 병상 0 (수용 가능하지만 확인 필요) -> needsCheckHospitals
    """
    ok, needs = [], []
    for h in hospitals:
        if h["lat"] == 0.0 or h["lng"] == 0.0:
            continue
        distance = haversine_km(lat, lng, h["lat"], h["lng"])
        if distance > radius:
            continue
        if disease_code != UNKNOWN_CODE and h["disease_status"].get(disease_code, "N") != "Y":
            continue
        (ok if h["emergency_beds"] > 0 else needs).append((distance, h))

    ok.sort(key=lambda x: x[0])
    needs.sort(key=lambda x: x[0])
    return (
        [_hospital_view(h, d, False) for d, h in ok[:MAX_RESULTS]],
        [_hospital_view(h, d, True) for d, h in needs[:MAX_NEEDS_CHECK]],
    )


def _radius_steps(status, found_index, specific):
    """프론트 RadiusStep[]: 반경 단계별 진행 표시 (+ 질환 특정 시 마지막에 '안정화 우선' 단계)."""
    steps = []
    for i, radius in enumerate(RADIUS_STAGES_KM):
        if status in ("ok", "radius_expanded"):
            steps.append({"label": f"{radius}km", "current": i == found_index, "passed": i < found_index})
        else:   # stabilize_first / none: 모든 반경에서 해당 질환 수용 병원을 못 찾음
            steps.append({"label": f"{radius}km", "current": False, "passed": True})
    if specific:
        steps.append({
            "label": "안정화 우선",
            "current": status == "stabilize_first",
            "passed": status == "none",
        })
    return steps


def _validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("요청은 JSON 객체여야 합니다.")
    category = payload.get("category")
    if not isinstance(category, str) or not category.strip():
        raise ValueError("category(주증상 범주)가 필요합니다.")
    try:
        grade = int(payload.get("grade"))
    except (TypeError, ValueError):
        raise ValueError("grade는 1~5 정수여야 합니다.")
    if not 1 <= grade <= 5:
        raise ValueError("grade는 1~5 정수여야 합니다.")
    try:
        lat, lng = float(payload.get("lat")), float(payload.get("lng"))
    except (TypeError, ValueError):
        raise ValueError("lat, lng는 숫자여야 합니다.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("lat, lng 범위가 올바르지 않습니다.")
    return category.strip(), grade, lat, lng


def match(payload):
    category, grade, lat, lng = _validate(payload)
    disease = classify_category(category)
    code = disease["code"]
    specific = code != UNKNOWN_CODE
    hospitals, meta = load_hospitals()

    def result(status, found_index, radius_km, ok, needs):
        return {
            "status": status,
            "chiefComplaint": disease["label"],
            "mappedConditions": disease["conditions"],
            "radiusSteps": _radius_steps(status, found_index, specific),
            "currentRadiusKm": radius_km,
            "hospitals": ok,
            "needsCheckHospitals": needs,
            # --- 아래는 MatchResult 타입에 없는 참고용 필드 ---
            "category": category,
            "categoryKnown": disease["category_known"],
            "grade": grade,
            "diseaseCode": code,
            "dataFetchedAt": meta["fetched_at"],   # 병상 데이터를 받은 시각 (epoch 초)
            "dataStale": meta["stale"],            # True면 공공 API 갱신 실패로 이전 데이터 사용 중
        }

    # 1) 해당 질환을 받을 수 있고 병상이 있는 병원을 반경 단계별로 탐색
    needs = []
    for i, radius in enumerate(RADIUS_STAGES_KM):
        ok, needs = evaluate(hospitals, lat, lng, radius, code)
        if ok:
            return result("ok" if i == 0 else "radius_expanded", i, radius, ok, needs)
    # (못 찾았으면 needs에는 가장 넓은 반경의 '병상 0이지만 수용 가능' 후보가 남아 있음)

    # 2) 질환 수용 병원이 없으면 질환 필터 없이 가까운 응급실(병상 있음)을 안정화 우선으로 추천
    if specific:
        for i, radius in enumerate(RADIUS_STAGES_KM):
            stab, _ = evaluate(hospitals, lat, lng, radius, UNKNOWN_CODE)
            if stab:
                return result("stabilize_first", None, radius, stab, needs)

    # 3) 그것도 없으면 none
    return result("none", None, RADIUS_STAGES_KM[-1], [], needs)


# ----------------------------------------------------------------------
# Lambda 진입점
# ----------------------------------------------------------------------
def _is_http_event(event):
    """API Gateway/Function URL 이벤트는 body가 JSON 문자열, 직접 invoke는 event가 곧 입력."""
    return isinstance(event, dict) and isinstance(event.get("body"), str)


def _parse_event(event):
    if not _is_http_event(event):
        return event
    body = event["body"]
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body or "{}")


def _respond(is_http, result, status_code):
    if is_http:
        return {
            "statusCode": status_code,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(result, ensure_ascii=False),
        }
    return result


def lambda_handler(event, context=None):
    is_http = _is_http_event(event)   # 파싱 실패 시에도 응답 형식(HTTP 여부)을 먼저 정해 둠
    try:
        payload = _parse_event(event)
        result = match(payload)
        status_code = 200
    except ValueError as e:                  # 입력 오류 (JSON 파싱 오류 포함)
        result = {"status": "ERROR", "success": False, "error": str(e)}
        status_code = 400
    except DataUnavailable as e:             # 공공 API 데이터를 못 가져옴
        print(f"[hc2-c-match] 데이터 오류: {e}")
        result = {"status": "ERROR", "success": False, "error": f"병원 데이터를 가져오지 못했습니다: {e}"}
        status_code = 503

    print(f"[hc2-c-match] status={result.get('status')} category={result.get('category')} "
          f"grade={result.get('grade')} radius={result.get('currentRadiusKm')}")
    return _respond(is_http, result, status_code)