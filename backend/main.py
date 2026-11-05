import os
import sys
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from math import radians, sin, cos, sqrt, atan2

# 상위 폴더 경로 추가하여 collector 임포트 가능하게 설정
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector.collector import fetch_hospital_data, save_to_local_db, get_hospitals_from_db

# .env 파일에서 환경변수 로드
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

app = FastAPI(title="구급차 실시간 음성 기반 병원 매칭 시스템")

# 서버가 켜질 때 초기 데이터를 한 번 수집해서 DB에 적재
@app.on_event("startup")
def startup_event():
    print("🚀 [FastAPI] 서버 시작: 초기 병원 데이터를 수집하여 DB에 동기화합니다.")
    initial_items = fetch_hospital_data()
    if initial_items:
        save_to_local_db(initial_items)

# --- [1] Haversine 거리 계산 함수 ---
def calculate_haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

# --- [2] 요청 데이터 모델 ---
class EmergencyRequest(BaseModel):
    ambulance_lat: float
    ambulance_lng: float
    transcribed_text: str  # STT로 변환된 대화 내용

# --- [3] 질환 코드 매핑 함수 ---
def analyze_text_to_disease_code(text: str) -> str:
    if "마비" in text or "팔다리" in text or "어눌" in text:
        return "stroke"  # 뇌졸중/뇌경색
    elif "가슴" in text or "심장" in text or "흉통" in text:
        return "cardiac" # 심근경색
    else:
        return "stroke"  # 기본값 예시

# --- [4] 병원 매칭 및 다단계 반경 확장 API 엔드포인트 ---
@app.post("/api/emergency-match")
def emergency_match(request: EmergencyRequest):
    # 1. 로컬 SQLite DB에서 최신 병원 데이터 조회
    hospital_db = get_hospitals_from_db()
    
    if not hospital_db:
        raise HTTPException(status_code=500, detail="로컬 DB에 저장된 병원 데이터가 없습니다.")

    # 2. 환자 대화에서 질환 코드 추출
    disease_code = analyze_text_to_disease_code(request.transcribed_text)
    
    # 3. 단계별 반경 설정 (예: 5km -> 15km -> 30km)
    radii = [5, 15, 30]
    checked_hospitals_log = []
    
    for radius in radii:
        matched_hospitals = []
        
        for hospital in hospital_db:
            if hospital["lat"] == 0.0 or hospital["lng"] == 0.0:
                continue
                
            distance = calculate_haversine_distance(
                request.ambulance_lat, 
                request.ambulance_lng, 
                hospital["lat"], 
                hospital["lng"]
            )
            
            if distance <= radius:
                has_beds = hospital["emergency_beds"] > 0
                can_accept = hospital["disease_status"].get(disease_code, "N") == "Y"
                
                hospital_info = {
                    "hospital_id": hospital["hospital_id"],
                    "hospital_name": hospital["hospital_name"],
                    "distance_km": round(distance, 2),
                    "emergency_beds": hospital["emergency_beds"],
                    "can_accept": can_accept
                }
                
                if hospital_info not in checked_hospitals_log:
                    checked_hospitals_log.append(hospital_info)
                
                if has_beds and can_accept:
                    matched_hospitals.append(hospital_info)
        
        if matched_hospitals:
            matched_hospitals.sort(key=lambda x: x["distance_km"])
            return {
                "status": "OK",
                "success": True,
                "detected_disease_code": disease_code,
                "radius_matched_km": radius,
                "results": matched_hospitals
            }
            
    return {
        "status": "NO_AVAILABLE_HOSPITAL",
        "success": False,
        "warning": "수용 가능 병원 없음. 구급상황관리센터 전달용 요약 정보를 생성합니다.",
        "detected_disease_code": disease_code,
        "checked_hospitals": checked_hospitals_log,
        "action_required": "상황실에 환자 상태 및 거절 사유를 일괄 전달하고 전원/안정화 병원 조율 요청"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)