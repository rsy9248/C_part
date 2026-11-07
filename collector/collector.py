import json
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

# 상위 폴더 경로 추가하여 rules 임포트 가능하게 설정
sys.path.append(str(Path(__file__).resolve().parent.parent))
from rules.disease_rules import disease_api_keys

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# .env에는 공공데이터포털의 '일반 인증키(Decoding)'를 넣으세요.
SERVICE_KEY = os.getenv("PUBLIC_API_KEY")

BASE_URL = "https://apis.data.go.kr/B552657/ErmctInfoInqireService"

# --- 응급의료기관 ---
OP_BEDS = "getEmrrmRltmUsefulSckbdInfoInqire"      # 응급실 실시간 가용병상정보 (수집 기준)
OP_SEVERE = "getSrsillDissAceptncPosblInfoInqire"  # 중증질환자 수용가능정보
OP_LIST = "getEgytListInfoInqire"                  # 응급의료기관 목록정보 (주소/전화/좌표)
OP_LC = "getEgytLcinfoInqire"                      # 응급의료기관 위치정보 (좌표 기준 검색, 실시간 호출)
OP_BASS = "getEgytBassInfoInqire"                  # 응급의료기관 기본정보 (HPID별 호출, 캐싱)
# --- 외상센터 ---
OP_STRM_LIST = "getStrmListInfoInqire"             # 외상센터 목록정보
OP_STRM_LC = "getStrmLcinfoInqire"                 # 외상센터 위치정보 (좌표 기준 검색, 실시간 호출)
OP_STRM_BASS = "getStrmBassInfoInqire"             # 외상센터 기본정보 (HPID별 호출, 캐싱)
# --- 메시지 ---
OP_MSG = "getEmrrmSrsillDissMsgInqire"             # 응급실 및 중증질환 메시지

# 특정 시도만 수집하고 싶으면 .env에 ER_REGION=인천광역시 처럼 추가 (비우면 전체)
# 파라미터 이름이 문서의 '요청변수' 표와 다르면 아래 REGION_PARAM을 수정하세요.
REGION = os.getenv("ER_REGION", "")
REGION_PARAM = {
    OP_BEDS: "STAGE1",
    OP_SEVERE: "STAGE1",
    OP_MSG: "STAGE1",
    OP_LIST: "Q0",
    OP_STRM_LIST: "Q0",
}

# 질환별 중증질환 수용가능 API 필드 (rules/disease_rules.json의 api_keys에서 로드)
# {"stroke": ["MKioskTy1", "MKioskTy2"], "cardiac": ["MKioskTy3"], ...}
# 값이 "Y"인 필드가 하나라도 있으면 수용 가능으로 판단합니다.
DISEASE_API_KEYS = disease_api_keys()

# 병상 응답의 장비/병상 필드 (필드 의미는 문서의 응답 명세로 확인!)
EQUIP_KEYS = {
    "ct_status": "hvctayn",
    "mri_status": "hvmriayn",
    "angio_status": "hvangioayn",
    "ventilator_status": "hvventiayn",
}

# True면 오퍼레이션별 첫 데이터의 필드명을 출력합니다 (신규 오퍼레이션 확인용)
DEBUG_KEYS = False

TIMEOUT = 10
ROWS_PER_PAGE = 100
MAX_PAGES = 10
DETAIL_TTL = 24 * 60 * 60  # 기본정보 캐시 유효 시간(초)

DB_PATH = Path(__file__).resolve().parent.parent / "hospitals.db"

# hospitals 테이블 컬럼 정의 (기존 DB에 없는 컬럼은 init_db에서 자동 추가됩니다)
COLUMNS = [
    ("hospital_id", "TEXT PRIMARY KEY"),
    ("hospital_name", "TEXT"),
    ("lat", "REAL"),
    ("lng", "REAL"),
    ("emergency_beds", "INTEGER"),
    ("stroke_status", "TEXT"),
    ("cardiac_status", "TEXT"),
    ("address", "TEXT"),
    ("tel_main", "TEXT"),
    ("tel_er", "TEXT"),
    ("emergency_class", "TEXT"),
    ("is_trauma_center", "INTEGER"),
    ("ct_status", "TEXT"),
    ("mri_status", "TEXT"),
    ("angio_status", "TEXT"),
    ("ventilator_status", "TEXT"),
    ("operating_rooms", "INTEGER"),
    ("general_beds", "INTEGER"),
    ("updated_at", "TEXT"),
    ("messages", "TEXT"),        # JSON 문자열
    ("disease_status", "TEXT"),  # JSON 문자열 {"stroke": "Y", "cardiac": "N", ...}
]
COLUMN_NAMES = [name for name, _ in COLUMNS]

DEFAULTS = {
    "hospital_name": "알 수 없는 병원",
    "lat": 0.0,
    "lng": 0.0,
    "emergency_beds": 0,
    "stroke_status": "N",
    "cardiac_status": "N",
    "address": "",
    "tel_main": "",
    "tel_er": "",
    "emergency_class": "",
    "is_trauma_center": 0,
    "ct_status": "?",
    "mri_status": "?",
    "angio_status": "?",
    "ventilator_status": "?",
    "operating_rooms": 0,
    "general_beds": 0,
    "updated_at": "",
    "messages": "[]",
    "disease_status": "{}",
}


# ----------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------
def init_db():
    """테이블 생성 + 기존 DB에 없는 컬럼 자동 추가(마이그레이션)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cols_sql = ", ".join(f"{name} {ctype}" for name, ctype in COLUMNS)
    cursor.execute(f"CREATE TABLE IF NOT EXISTS hospitals ({cols_sql})")

    existing = {row[1] for row in cursor.execute("PRAGMA table_info(hospitals)").fetchall()}
    for name, ctype in COLUMNS:
        if name not in existing:
            cursor.execute(f"ALTER TABLE hospitals ADD COLUMN {name} {ctype.replace(' PRIMARY KEY', '')}")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hospital_detail (
            hpid TEXT,
            kind TEXT,
            data TEXT,
            cached_at REAL,
            PRIMARY KEY (hpid, kind)
        )
    """)
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------
# 공공 API 호출 / 파싱
# ----------------------------------------------------------------------
def _parse_response(resp):
    """XML 또는 JSON 응답을 (items, totalCount)로 변환. 실패하면 (None, 0)."""
    text = resp.text.strip()

    if text.startswith("<"):
        root = ET.fromstring(text)

        # 게이트웨이 에러 형식(OpenAPI_ServiceResponse)과 정상 형식 모두 대응
        code = root.findtext(".//resultCode")
        if code is None:
            code = root.findtext(".//returnReasonCode")
        if code not in ("00", "0"):
            msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg")
            print(f"⚠️ [Collector] API 에러 코드={code}, 메시지={msg}")
            return None, 0

        items = [
            {child.tag: (child.text or "").strip() for child in item}
            for item in root.findall(".//item")
        ]
        total = int(root.findtext(".//totalCount") or len(items))
        return items, total

    data = resp.json()
    response = data.get("response", {})
    code = str(response.get("header", {}).get("resultCode", ""))
    if code not in ("00", "0"):
        print(f"⚠️ [Collector] API 에러 코드={code}, 메시지={response.get('header', {}).get('resultMsg')}")
        return None, 0

    body = response.get("body", {})
    raw = body.get("items", {})
    raw = raw.get("item", []) if isinstance(raw, dict) else []
    items = [raw] if isinstance(raw, dict) else raw
    return items, int(body.get("totalCount", len(items)))


def _fetch_operation(operation, extra_params=None, region=True, single_page=False):
    """
    한 오퍼레이션을 호출해 item 리스트를 반환. 실패하면 None.
    - extra_params: 오퍼레이션별 추가 요청변수 (예: {"HPID": "A1100010"})
    - region: True면 ER_REGION 설정을 지역 파라미터로 전달
    - single_page: True면 1페이지만 호출 (기본정보/위치검색처럼 결과가 적은 호출용)
    """
    url = f"{BASE_URL}/{operation}"
    collected = []
    last_page = 1 if single_page else MAX_PAGES

    for page in range(1, last_page + 1):
        params = {
            "serviceKey": SERVICE_KEY,
            "pageNo": str(page),
            "numOfRows": str(ROWS_PER_PAGE),
        }
        if region and REGION and operation in REGION_PARAM:
            params[REGION_PARAM[operation]] = REGION
        if extra_params:
            params.update(extra_params)

        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"🚨 [Collector] {operation} 통신 에러: {e}")
            return None

        print(f"🔍 [Collector] {operation} p{page} 응답 코드: {resp.status_code}")
        if resp.status_code != 200:
            print(f"   응답 본문: {resp.text[:300]}")  # 400 원인 확인용
            return None

        try:
            items, total = _parse_response(resp)
        except Exception as e:
            print(f"⚠️ [Collector] {operation} 파싱 에러: {e}")
            print(f"   응답 본문: {resp.text[:300]}")
            return None

        if items is None:
            return None
        if not items:
            break

        collected.extend(items)
        if len(collected) >= total:
            break

    if DEBUG_KEYS and collected:
        print(f"🧪 [Collector] {operation} 필드명: {list(collected[0].keys())}")
    return collected


# ----------------------------------------------------------------------
# 값 변환 유틸
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


def _index_by_hpid(items):
    return {_get(i, "hpid"): i for i in (items or []) if _get(i, "hpid")}


# ----------------------------------------------------------------------
# 일괄 수집 + 병합
# ----------------------------------------------------------------------
def fetch_hospital_data(allow_dummy=True):
    """
    여러 오퍼레이션을 hpid 기준으로 합쳐 DB 저장용 리스트를 반환.
    - allow_dummy=True : 수집 실패 시 테스트용 더미 데이터를 반환 (개발/최초 실행용)
    - allow_dummy=False: 수집 실패 시 빈 리스트를 반환 (주기적 갱신용, 더미가 DB에 섞이는 것 방지)
    """
    if not SERVICE_KEY:
        print("🚨 [Collector] .env 파일에 PUBLIC_API_KEY가 설정되지 않았습니다.")
        return get_dummy_items() if allow_dummy else []

    print("🔄 [Collector] 공공 API에서 실시간 병원 데이터 수집 시도 중...")

    beds = _fetch_operation(OP_BEDS)
    if not beds:
        if allow_dummy:
            print("⚠️ [Collector] 병상 정보 수집 실패, 테스트용 더미 데이터를 대신 사용합니다.")
            return get_dummy_items()
        print("⚠️ [Collector] 병상 정보 수집 실패, 더미 데이터는 사용하지 않고 기존 DB를 유지합니다.")
        return []

    # 아래 오퍼레이션은 실패해도 병상 정보만으로 계속 진행합니다.
    optional = {
        "중증질환 수용정보 (질환별 수용 상태가 'N'으로 저장됨)": _fetch_operation(OP_SEVERE),
        "응급의료기관 목록 (좌표/주소/전화 비어 있음)": _fetch_operation(OP_LIST),
        "외상센터 목록 (외상센터 표시 안 됨)": _fetch_operation(OP_STRM_LIST),
        "응급실/중증질환 메시지 (메시지 비어 있음)": _fetch_operation(OP_MSG),
    }
    for label, result in optional.items():
        if result is None:
            print(f"⚠️ [Collector] {label} 수집 실패")

    severe_by_id = _index_by_hpid(optional["중증질환 수용정보 (질환별 수용 상태가 'N'으로 저장됨)"])
    loc_by_id = _index_by_hpid(optional["응급의료기관 목록 (좌표/주소/전화 비어 있음)"])
    strm_by_id = _index_by_hpid(optional["외상센터 목록 (외상센터 표시 안 됨)"])

    msgs_by_id = {}
    for m in optional["응급실/중증질환 메시지 (메시지 비어 있음)"] or []:
        hid = _get(m, "hpid")
        if hid:
            msgs_by_id.setdefault(hid, []).append(m)

    hospitals = []
    for item in beds:
        hpid = _get(item, "hpid")
        if not hpid:
            continue

        sev = severe_by_id.get(hpid, {})
        loc = loc_by_id.get(hpid, {})
        strm = strm_by_id.get(hpid, {})

        lat = _to_float(_get(loc, "wgs84Lat", "latitude", "lat"))
        lng = _to_float(_get(loc, "wgs84Lon", "longitude", "lon", "lng"))
        if lat == 0.0 and lng == 0.0 and strm:  # 응급기관 목록에 좌표가 없으면 외상센터 목록으로 보완
            lat = _to_float(_get(strm, "wgs84Lat", "latitude", "lat"))
            lng = _to_float(_get(strm, "wgs84Lon", "longitude", "lon", "lng"))

        # 질환별 수용 가능 여부 {"stroke": "Y", "cardiac": "N", ...}
        status = {code: _any_yes(sev, keys) for code, keys in DISEASE_API_KEYS.items()}

        h = {
            "hospital_id": hpid,
            "hospital_name": _get(item, "dutyName") or _get(loc, "dutyName", default=DEFAULTS["hospital_name"]),
            "lat": lat,
            "lng": lng,
            "emergency_beds": _to_int(_get(item, "hvec")),
            "disease_status": json.dumps(status),
            "stroke_status": status.get("stroke", "N"),    # 기존 컬럼 호환용
            "cardiac_status": status.get("cardiac", "N"),  # 기존 컬럼 호환용
            "address": _get(loc, "dutyAddr") or _get(strm, "dutyAddr"),
            "tel_main": _get(loc, "dutyTel1") or _get(strm, "dutyTel1"),
            "tel_er": _get(item, "dutyTel3") or _get(loc, "dutyTel3"),
            "emergency_class": _get(loc, "dutyEmclsName"),
            "is_trauma_center": 1 if strm else 0,
            "operating_rooms": _to_int(_get(item, "hvoc")),
            "general_beds": _to_int(_get(item, "hvgc")),
            "updated_at": _get(item, "hvidate"),
            "messages": json.dumps(msgs_by_id.get(hpid, []), ensure_ascii=False),
        }
        for column, source_key in EQUIP_KEYS.items():
            h[column] = _yn(item, source_key)
        hospitals.append(h)

    no_coord = sum(1 for h in hospitals if h["lat"] == 0.0 and h["lng"] == 0.0)
    trauma = sum(h["is_trauma_center"] for h in hospitals)
    with_msg = sum(1 for h in hospitals if h["messages"] != "[]")
    print(
        f"✅ [Collector] 공공 API 데이터 수집 성공! (총 {len(hospitals)}개 병원, "
        f"좌표 없음 {no_coord}개, 외상센터 {trauma}개, 메시지 있는 병원 {with_msg}개)"
    )
    if hospitals and no_coord == len(hospitals):
        print("⚠️ [Collector] 모든 병원의 좌표가 비어 있습니다. 필드명(DEBUG_KEYS 출력)을 확인하세요.")
    return hospitals


# ----------------------------------------------------------------------
# 실시간/지연 호출 (일괄 수집에 포함하지 않는 오퍼레이션)
# ----------------------------------------------------------------------
def fetch_hospital_detail(hpid, trauma=False, force=False):
    """
    병원 1곳의 기본정보(getEgytBassInfoInqire / getStrmBassInfoInqire).
    24시간 캐시(hospital_detail 테이블)를 사용합니다. 실패하면 None.
    """
    init_db()
    kind = "trauma" if trauma else "emergency"

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT data, cached_at FROM hospital_detail WHERE hpid=? AND kind=?", (hpid, kind)
    ).fetchone()
    conn.close()
    if row and not force and time.time() - row[1] < DETAIL_TTL:
        return json.loads(row[0])

    items = _fetch_operation(
        OP_STRM_BASS if trauma else OP_BASS,
        extra_params={"HPID": hpid},
        region=False,
        single_page=True,
    )
    if not items:
        # 호출이 실패했지만 오래된 캐시가 있으면 그것이라도 반환
        return json.loads(row[0]) if row else None

    detail = items[0]
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO hospital_detail (hpid, kind, data, cached_at) VALUES (?, ?, ?, ?)",
        (hpid, kind, json.dumps(detail, ensure_ascii=False), time.time()),
    )
    conn.commit()
    conn.close()
    return detail


def search_nearby(lat, lng, trauma=False, limit=10):
    """
    좌표 기준 가까운 기관 검색(getEgytLcinfoInqire / getStrmLcinfoInqire).
    DB 기준 거리 계산 대신 공공 API 결과를 그대로 보고 싶을 때 사용합니다. 실패하면 [].
    """
    items = _fetch_operation(
        OP_STRM_LC if trauma else OP_LC,
        extra_params={"WGS84_LON": str(lng), "WGS84_LAT": str(lat)},
        region=False,
        single_page=True,
    )
    return (items or [])[:limit]


# ----------------------------------------------------------------------
# 더미 데이터 / 저장 / 조회
# ----------------------------------------------------------------------
def _with_defaults(h):
    merged = dict(DEFAULTS)
    merged.update(h)
    return merged


def get_dummy_items():
    """공공 API 통신 실패 시 백업용 더미 데이터"""
    return [
        _with_defaults({
            "hospital_id": "H001",
            "hospital_name": "강원대학교병원",
            "lat": 37.8655,
            "lng": 127.7485,
            "emergency_beds": 5,
            "stroke_status": "Y",
            "cardiac_status": "Y",
        }),
        _with_defaults({
            "hospital_id": "H002",
            "hospital_name": "인천길병원 (테스트용)",
            "lat": 37.4478,
            "lng": 126.7025,
            "emergency_beds": 10,
            "stroke_status": "Y",
            "cardiac_status": "Y",
        }),
    ]


def save_to_local_db(items):
    """병합된 데이터를 SQLite DB에 저장/업데이트(UPSERT)"""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    placeholders = ", ".join("?" for _ in COLUMN_NAMES)
    updates = ", ".join(f"{c}=excluded.{c}" for c in COLUMN_NAMES if c != "hospital_id")
    sql = (
        f"INSERT INTO hospitals ({', '.join(COLUMN_NAMES)}) VALUES ({placeholders}) "
        f"ON CONFLICT(hospital_id) DO UPDATE SET {updates}"
    )

    count = 0
    for h in items:
        try:
            h = _with_defaults(h)
            cursor.execute(sql, tuple(h[c] for c in COLUMN_NAMES))
            count += 1
        except Exception as e:
            print(f"⚠️ [Collector] 저장 실패 ({h.get('hospital_id')}): {e}")

    conn.commit()
    conn.close()
    print(f"💾 [Collector] SQLite DB 동기화 완료! (총 {count}개 병원 반영)\n")


def get_hospitals_from_db():
    """FastAPI 서버에서 호출할 수 있도록 DB에서 병원 목록을 읽어오는 함수"""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM hospitals").fetchall()
    conn.close()

    hospitals = []
    for row in rows:
        try:
            messages = json.loads(row["messages"] or "[]")
        except json.JSONDecodeError:
            messages = []

        try:
            status = json.loads(row["disease_status"] or "{}")
        except json.JSONDecodeError:
            status = {}
        if not status:  # 마이그레이션 직후 구버전 행/더미 데이터 대비
            status = {"stroke": row["stroke_status"], "cardiac": row["cardiac_status"]}

        hospitals.append({
            # 기존 응답 형식 (main.py 호환)
            "hospital_id": row["hospital_id"],
            "hospital_name": row["hospital_name"],
            "lat": row["lat"],
            "lng": row["lng"],
            "emergency_beds": row["emergency_beds"],
            "disease_status": status,
            # 추가 정보
            "address": row["address"],
            "tel_main": row["tel_main"],
            "tel_er": row["tel_er"],
            "emergency_class": row["emergency_class"],
            "is_trauma_center": bool(row["is_trauma_center"]),
            "equipment": {
                "ct": row["ct_status"],
                "mri": row["mri_status"],
                "angio": row["angio_status"],
                "ventilator": row["ventilator_status"],
            },
            "operating_rooms": row["operating_rooms"],
            "general_beds": row["general_beds"],
            "updated_at": row["updated_at"],
            "messages": messages,
        })
    return hospitals


if __name__ == "__main__":
    # 단독 실행으로 수집 → 저장 → 조회까지 확인
    save_to_local_db(fetch_hospital_data())
    for h in get_hospitals_from_db()[:3]:
        print(h)
