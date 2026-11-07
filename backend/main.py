import asyncio
import os
import sys
import threading
import time
import uuid
from math import radians, sin, cos, sqrt, atan2
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# 상위 폴더 경로 추가하여 collector 임포트 가능하게 설정
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector.collector import fetch_hospital_data, save_to_local_db, get_hospitals_from_db
from rules.disease_rules import (
    UNKNOWN_CODE,
    MAPPING_RULES_TEXT,
    classify_category,
    build_mapping_table,
    build_category_table,
    print_disease_mapping,
)

# .env 파일에서 환경변수 로드
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

app = FastAPI(title="구급차 실시간 음성 기반 병원 매칭 시스템")

# CORS: 프론트(다른 주소/포트)에서 이 API를 호출할 수 있게 허용.
# 개발 중에는 전체 허용(*). 배포/발표 때는 .env에 CORS_ORIGINS=http://localhost:3000,http://192.168.0.10:3000 처럼 지정.
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,   # "*"와 credentials=True는 브라우저가 거부하므로 사용하지 않음
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================================================================
# 설정값
# =====================================================================
RADIUS_STAGES_KM = [5, 15, 30, 50]   # 반경 단계
EXPAND_INTERVAL_SEC = 5 * 60         # 5분 경과할 때마다 한 단계씩 반경 확대
SESSION_TTL_SEC = 2 * 60 * 60        # 종료되지 않은 출동 세션 보관 시간 (2시간)
# 병원 데이터 주기적 갱신 간격(초). .env에 REFRESH_INTERVAL_SEC=600 처럼 지정 가능 (공공 API 호출 한도 고려)
REFRESH_INTERVAL_SEC = int(os.getenv("REFRESH_INTERVAL_SEC", "300"))
# 병원이 수락/거절 응답을 하지 않을 때 자동으로 거절 처리하고 다음 병원으로 넘기는 시간(초)
HOSPITAL_RESPONSE_TIMEOUT_SEC = int(os.getenv("HOSPITAL_RESPONSE_TIMEOUT_SEC", "90"))


# 데이터 갱신 상태 (GET /api/data-status 에서 확인)
data_status = {
    "last_success_at": None,   # 마지막 갱신 성공 시각 (epoch 초)
    "last_attempt_at": None,
    "last_count": 0,
    "consecutive_failures": 0,
}


def refresh_hospital_data(allow_dummy: bool = False) -> bool:
    """공공 API에서 병원 데이터를 수집해 DB에 반영. 실패하면 기존 DB를 유지하고 False 반환."""
    data_status["last_attempt_at"] = time.time()
    items = fetch_hospital_data(allow_dummy=allow_dummy)
    if not items:
        data_status["consecutive_failures"] += 1
        print(f"[Refresh] 갱신 실패 (연속 {data_status['consecutive_failures']}회). 기존 DB 데이터를 유지합니다.")
        return False

    save_to_local_db(items)
    data_status.update(last_success_at=time.time(), last_count=len(items), consecutive_failures=0)
    return True


async def refresh_loop():
    """REFRESH_INTERVAL_SEC마다 병원 데이터를 백그라운드에서 갱신."""
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SEC)
        try:
            await asyncio.to_thread(refresh_hospital_data)
        except Exception as e:  # 한 번의 오류로 갱신 루프가 죽지 않도록
            data_status["consecutive_failures"] += 1
            print(f"[Refresh] 예기치 못한 오류: {e}")


# 서버가 켜질 때 초기 데이터를 한 번 수집해서 DB에 적재 + 주기적 갱신 시작
@app.on_event("startup")
async def startup_event():
    print_disease_mapping()
    print("[FastAPI] 서버 시작: 초기 병원 데이터를 수집하여 DB에 동기화합니다.")
    # DB가 비어 있을 때만(최초 실행) 더미 데이터를 허용. 기존 실데이터가 있으면 더미로 덮이지 않게 함.
    db_is_empty = not await asyncio.to_thread(get_hospitals_from_db)
    await asyncio.to_thread(refresh_hospital_data, db_is_empty)
    app.state.refresh_task = asyncio.create_task(refresh_loop())
    print(f"[FastAPI] 병원 데이터 자동 갱신 시작 ({REFRESH_INTERVAL_SEC}초 간격)")


@app.on_event("shutdown")
async def shutdown_event():
    task = getattr(app.state, "refresh_task", None)
    if task:
        task.cancel()


# =====================================================================
# [2] Haversine 거리 계산
# =====================================================================
def calculate_haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c


# =====================================================================
# [3] 요청 모델
# =====================================================================
class EmergencyRequest(BaseModel):
    # A 파트 일지에서 받는 값 (환자 이름·전화번호가 담긴 STT 원문은 받지 않음)
    category: str                       # 주증상 범주 (예: "chest pain", "neurologic deficit" ...)
    grade: int = Field(..., ge=1, le=5)  # B 등급 1~5
    lat: float                          # 구급차 위도
    lng: float                          # 구급차 경도


class NextRequest(BaseModel):
    rejected_hospital_id: Optional[str] = None  # 방금 거절한 병원 (없으면 단순 갱신)
    lat: Optional[float] = None                 # 이동 중이면 최신 위치 전달
    lng: Optional[float] = None


class HospitalResponse(BaseModel):
    accept: bool                     # True: 수락, False: 거절
    reason: Optional[str] = None     # 거절 사유 (선택)


# =====================================================================
# [4] 매칭 로직
# =====================================================================
# 출동 세션: {case_id: {...}}  (서버 재시작 시 초기화되는 인메모리 저장소)
sessions: dict = {}
# 병원 수락 요청 인덱스: {request_id: case_id}
requests_index: dict = {}
# 여러 요청(대원 화면/병원 화면)이 동시에 상태를 바꿔도 꼬이지 않도록 하는 잠금
state_lock = threading.RLock()


def purge_old_sessions():
    now = time.time()
    for cid in [c for c, s in sessions.items() if now - s["started_at"] > SESSION_TTL_SEC]:
        case = sessions.pop(cid, None)
        for req in (case or {}).get("requests", []):
            requests_index.pop(req["request_id"], None)


def search_in_radius(hospital_db, lat, lng, radius, disease_code, rejected_ids):
    """반경 내 병원을 검사해 (수용 가능 목록, 검사한 전체 목록)을 반환."""
    matched, checked = [], []

    for h in hospital_db:
        if h["lat"] == 0.0 or h["lng"] == 0.0:
            continue

        distance = calculate_haversine_distance(lat, lng, h["lat"], h["lng"])
        if distance > radius:
            continue

        has_beds = h["emergency_beds"] > 0
        if disease_code == UNKNOWN_CODE:
            can_accept = True
        else:
            can_accept = h["disease_status"].get(disease_code, "N") == "Y"

        info = {
            "hospital_id": h["hospital_id"],
            "hospital_name": h["hospital_name"],
            "distance_km": round(distance, 2),
            "emergency_beds": h["emergency_beds"],
            "can_accept": can_accept,
            "rejected": h["hospital_id"] in rejected_ids,
            "tel_er": h.get("tel_er", ""),
            "address": h.get("address", ""),
            "updated_at": h.get("updated_at", ""),
        }
        checked.append(info)

        if has_beds and can_accept and not info["rejected"]:
            matched.append(info)

    matched.sort(key=lambda x: x["distance_km"])
    checked.sort(key=lambda x: x["distance_km"])
    return matched, checked


def current_time_stage(case: dict) -> int:
    """출동 후 경과 시간에 따른 반경 단계 (5분마다 +1)."""
    elapsed = time.time() - case["started_at"]
    return min(int(elapsed // EXPAND_INTERVAL_SEC), len(RADIUS_STAGES_KM) - 1)


def build_base(case_id: str, case: dict) -> dict:
    """응답에 항상 들어가는 공통 정보."""
    elapsed = time.time() - case["started_at"]
    time_stage = current_time_stage(case)
    last_stage = len(RADIUS_STAGES_KM) - 1
    return {
        "case_id": case_id,
        "detected_disease_code": case["disease"]["code"],
        "detected_disease_name": case["disease"]["name"],
        "category": case["category"],
        "category_known": case["disease"]["category_known"],
        "grade": case["grade"],
        "elapsed_sec": int(elapsed),
        "next_expand_in_sec": (
            None if time_stage >= last_stage
            else int(EXPAND_INTERVAL_SEC - (elapsed % EXPAND_INTERVAL_SEC))
        ),
        "rejected_hospital_ids": sorted(case["rejected_ids"]),
    }


def run_match(case_id: str) -> dict:
    """
    세션의 현재 상태로 병원을 재탐색한다.
    - 시간 기반 반경: 출동 후 5분 경과할 때마다 반경 한 단계 확대
    - 해당 반경에 후보가 없으면 즉시 다음 단계로 자동 확대
    - 후보는 거리순 정렬, 그중 가장 가까운 1곳을 best_hospital로 반환
    """
    case = sessions[case_id]
    hospital_db = get_hospitals_from_db()
    if not hospital_db:
        raise HTTPException(status_code=500, detail="로컬 DB에 저장된 병원 데이터가 없습니다.")

    time_stage = current_time_stage(case)
    base = build_base(case_id, case)

    checked = []
    for stage in range(time_stage, len(RADIUS_STAGES_KM)):
        radius = RADIUS_STAGES_KM[stage]
        matched, checked = search_in_radius(
            hospital_db,
            case["lat"],
            case["lng"],
            radius,
            case["disease"]["code"],
            case["rejected_ids"],
        )
        if matched:
            return {
                **base,
                "status": "OK",
                "success": True,
                "radius_matched_km": radius,
                "time_based_radius_km": RADIUS_STAGES_KM[time_stage],
                "auto_expanded": stage > time_stage,
                "best_hospital": matched[0],   # 가장 가까운 1곳
                "candidates": matched,         # 거리순 전체 후보
            }

    return {
        **base,
        "status": "NO_AVAILABLE_HOSPITAL",
        "success": False,
        "warning": "수용 가능 병원 없음. 구급상황관리센터 전달용 요약 정보를 생성합니다.",
        "checked_hospitals": checked,
        "action_required": "상황실에 환자 상태 및 거절 사유를 일괄 전달하고 전원/안정화 병원 조율 요청",
    }


# =====================================================================
# [5] 병원 수락 요청 흐름
#   매칭 → 가장 가까운 병원에 수락 요청(PENDING)
#        → 병원이 수락(ACCEPTED) / 거절(REJECTED) / 무응답(TIMEOUT)
#        → 거절·무응답이면 그 병원을 제외하고 다음으로 가까운 병원에 자동 재요청
# =====================================================================
REQ_PENDING = "PENDING"
REQ_ACCEPTED = "ACCEPTED"
REQ_REJECTED = "REJECTED"
REQ_TIMEOUT = "TIMEOUT"
REQ_CANCELED = "CANCELED"   # 구급대원이 직접 다른 병원으로 넘긴 경우


def get_case(case_id: str) -> dict:
    if case_id not in sessions:
        raise HTTPException(status_code=404, detail="존재하지 않거나 만료된 출동 세션입니다.")
    return sessions[case_id]


def send_request_to_hospital(case_id: str, hospital: dict) -> dict:
    """해당 병원에 수락 여부 요청을 만든다 (상태: PENDING)."""
    case = sessions[case_id]
    req = {
        "request_id": uuid.uuid4().hex[:10],
        "case_id": case_id,
        "hospital_id": hospital["hospital_id"],
        "hospital_name": hospital["hospital_name"],
        "hospital": hospital,   # 요청 시점의 병원 정보 (거리/병상/전화번호 등)
        "status": REQ_PENDING,
        "sent_at": time.time(),
        "responded_at": None,
        "reject_reason": None,
    }
    case["requests"].append(req)
    case["current_request"] = req
    requests_index[req["request_id"]] = case_id
    return req


def dispatch_next(case_id: str) -> dict:
    """현재 조건으로 병원을 다시 찾아 가장 가까운 1곳에 요청 전송. 후보가 없으면 current_request를 비운다."""
    match = run_match(case_id)
    case = sessions[case_id]
    if match["status"] == "OK":
        send_request_to_hospital(case_id, match["best_hospital"])
    else:
        case["current_request"] = None
    return match


def cancel_current_request(case: dict):
    """구급대원이 현재 병원을 건너뛸 때: 진행 중인 요청을 취소하고 그 병원을 제외 목록에 넣는다."""
    req = case.get("current_request")
    if req and req["status"] in (REQ_PENDING, REQ_ACCEPTED):
        req["status"] = REQ_CANCELED
        req["responded_at"] = time.time()
        case["rejected_ids"].add(req["hospital_id"])
    case["current_request"] = None


def expire_pending_requests():
    """응답 제한 시간이 지난 요청을 TIMEOUT 처리하고 다음 병원으로 넘긴다."""
    now = time.time()
    for case_id, case in list(sessions.items()):
        req = case.get("current_request")
        if req and req["status"] == REQ_PENDING and now - req["sent_at"] > HOSPITAL_RESPONSE_TIMEOUT_SEC:
            req["status"] = REQ_TIMEOUT
            req["responded_at"] = now
            case["rejected_ids"].add(req["hospital_id"])
            dispatch_next(case_id)


def case_status_of(case: dict) -> str:
    req = case.get("current_request")
    if req is None:
        return "NO_AVAILABLE_HOSPITAL"
    return "ACCEPTED" if req["status"] == REQ_ACCEPTED else "WAITING_HOSPITAL"


def public_request(req: dict) -> dict:
    now = time.time()
    out = {
        "request_id": req["request_id"],
        "hospital_id": req["hospital_id"],
        "hospital_name": req["hospital_name"],
        "status": req["status"],
        "sent_at": int(req["sent_at"]),
        "responded_at": int(req["responded_at"]) if req["responded_at"] else None,
        "reject_reason": req["reject_reason"],
        "waiting_sec": int((req["responded_at"] or now) - req["sent_at"]),
    }
    if req["status"] == REQ_PENDING:
        out["timeout_in_sec"] = max(0, int(HOSPITAL_RESPONSE_TIMEOUT_SEC - (now - req["sent_at"])))
    return out


def status_message(case: dict) -> str:
    req = case.get("current_request")
    status = case_status_of(case)
    if status == "ACCEPTED":
        tel = req["hospital"].get("tel_er") or "정보 없음"
        return f"{req['hospital_name']} 수락 - 이 병원으로 이동하세요. (응급실 전화: {tel})"
    if status == "WAITING_HOSPITAL":
        return f"{req['hospital_name']}에 수용 가능 여부를 문의했습니다. 응답을 기다리는 중입니다."
    return "수용 가능한 병원이 없습니다. 상황실에 환자 상태를 전달하고 병원 조율을 요청하세요."


def case_view(case_id: str, match: Optional[dict] = None) -> dict:
    """구급대원 화면용 응답: 선정된 병원 + 요청 상태 + (수락 전이면) 거리순 후보."""
    case = get_case(case_id)
    cur = case["current_request"]

    if cur and cur["status"] == REQ_ACCEPTED:
        view = {**build_base(case_id, case), "success": True}
    else:
        view = match if match is not None else run_match(case_id)
        view = dict(view)
        # 후보가 없어서 대기 중이던 건이 시간 경과(반경 확대)나 병상 갱신으로 후보가 생기면 자동 요청
        if cur is None and view["status"] == "OK":
            cur = send_request_to_hospital(case_id, view["best_hospital"])

    view.update({
        "case_status": case_status_of(case),
        "message": status_message(case),
        "selected_hospital": cur["hospital"] if cur else None,
        "current_request": public_request(cur) if cur else None,
        "request_history": [public_request(r) for r in case["requests"]],
    })
    return view


def hospital_request_view(req: dict) -> dict:
    """병원 화면용: 환자 요약 + 구급차 정보 포함."""
    case = sessions[req["case_id"]]
    return {
        **public_request(req),
        "case_id": req["case_id"],
        "patient": {
            "disease_code": case["disease"]["code"],
            "disease_name": case["disease"]["name"],
            "category": case["category"],
            "grade": case["grade"],
        },
        "ambulance": {
            "lat": case["lat"],
            "lng": case["lng"],
            "distance_km": req["hospital"]["distance_km"],
        },
    }


# =====================================================================
# [6] API 엔드포인트
# =====================================================================
@app.post("/api/emergency-match")
def emergency_match(request: EmergencyRequest):
    """출동 세션 시작: 가장 가까운 수용 가능 병원을 찾아 수락 요청까지 자동 전송."""
    with state_lock:
        purge_old_sessions()

        case_id = uuid.uuid4().hex[:12]
        sessions[case_id] = {
            "started_at": time.time(),
            "lat": request.lat,
            "lng": request.lng,
            "category": request.category,
            "grade": request.grade,
            "disease": classify_category(request.category),
            "rejected_ids": set(),
            "requests": [],            # 병원에 보낸 요청 이력
            "current_request": None,   # 현재 진행 중인 요청
        }
        match = dispatch_next(case_id)
        return case_view(case_id, match)


@app.get("/api/emergency-match/{case_id}")
def emergency_match_refresh(case_id: str):
    """구급대원 화면 폴링용: 요청 상태(대기/수락)와 선정된 병원, 반경 확대 결과를 반환."""
    with state_lock:
        expire_pending_requests()
        get_case(case_id)
        return case_view(case_id)


@app.post("/api/emergency-match/{case_id}/next")
def emergency_match_next(case_id: str, body: NextRequest):
    """구급대원이 현재 병원을 건너뛰고 다음으로 가까운 병원에 요청 (병원 거절은 자동 처리되므로 보통 불필요)."""
    with state_lock:
        expire_pending_requests()
        case = get_case(case_id)
        if body.rejected_hospital_id:
            case["rejected_ids"].add(body.rejected_hospital_id)
        if body.lat is not None and body.lng is not None:
            case["lat"], case["lng"] = body.lat, body.lng
        cancel_current_request(case)
        match = dispatch_next(case_id)
        return case_view(case_id, match)


@app.delete("/api/emergency-match/{case_id}")
def emergency_match_close(case_id: str):
    """환자 이송이 끝나면 세션 종료."""
    with state_lock:
        case = sessions.pop(case_id, None)
        for req in (case or {}).get("requests", []):
            requests_index.pop(req["request_id"], None)
    return {"closed": True}


@app.get("/api/hospitals/{hospital_id}/requests")
def hospital_requests(hospital_id: str, status: str = "PENDING"):
    """병원 화면용: 이 병원으로 들어온 요청 목록. status=ALL 이면 처리된 요청도 포함."""
    with state_lock:
        expire_pending_requests()
        results = []
        for case in sessions.values():
            for req in case["requests"]:
                if req["hospital_id"] != hospital_id:
                    continue
                if status.upper() != "ALL" and req["status"] != status.upper():
                    continue
                results.append(hospital_request_view(req))
        results.sort(key=lambda r: r["sent_at"])
        return {"hospital_id": hospital_id, "count": len(results), "requests": results}


@app.post("/api/requests/{request_id}/respond")
def hospital_respond(request_id: str, body: HospitalResponse):
    """병원이 수락/거절. 거절하면 다음으로 가까운 병원에 자동으로 재요청한다."""
    with state_lock:
        expire_pending_requests()

        case_id = requests_index.get(request_id)
        if case_id is None or case_id not in sessions:
            raise HTTPException(status_code=404, detail="존재하지 않거나 만료된 요청입니다.")
        case = sessions[case_id]
        req = next(r for r in case["requests"] if r["request_id"] == request_id)

        if req["status"] != REQ_PENDING:
            raise HTTPException(status_code=409, detail=f"이미 처리된 요청입니다. (현재 상태: {req['status']})")

        req["responded_at"] = time.time()

        if body.accept:
            req["status"] = REQ_ACCEPTED
            return {
                "request": public_request(req),
                "case_status": "ACCEPTED",
                "message": status_message(case),
            }

        req["status"] = REQ_REJECTED
        req["reject_reason"] = body.reason
        case["rejected_ids"].add(req["hospital_id"])
        dispatch_next(case_id)
        nxt = case["current_request"]
        return {
            "request": public_request(req),
            "case_status": case_status_of(case),
            "next_request": public_request(nxt) if nxt else None,
            "message": "거절 처리되었습니다. " + status_message(case),
        }


@app.get("/api/data-status")
def get_data_status():
    """병원 데이터 갱신 상태 조회 (마지막 성공 시각, 연속 실패 횟수 등)."""
    now = time.time()
    last = data_status["last_success_at"]
    return {
        **data_status,
        "seconds_since_last_success": int(now - last) if last else None,
        "refresh_interval_sec": REFRESH_INTERVAL_SEC,
    }


@app.get("/api/disease-codes")
def get_disease_codes():
    """질환 코드 매핑표와 매핑 규칙 조회."""
    return {
        "unknown_code": UNKNOWN_CODE,
        "table": build_mapping_table(),
        "category_table": build_category_table(),
        "rules": MAPPING_RULES_TEXT,
        "radius_stages_km": RADIUS_STAGES_KM,
        "expand_interval_sec": EXPAND_INTERVAL_SEC,
    }


if __name__ == "__main__":
    import uvicorn
    # 0.0.0.0: 같은 네트워크의 팀원 PC에서도 접속 가능 (http://내PC_IP:8000). 내 PC에서만 쓰려면 .env에 HOST=127.0.0.1
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )