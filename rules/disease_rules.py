"""
질환 코드 매핑 규칙 로더 / 분류기.

팀원이 의학용어(키워드)나 질환 코드를 추가할 때는 이 파일이 아니라
disease_rules.json만 수정하면 됩니다. (수정 후 서버 재시작)

JSON 항목
- name     : 질환 표시 이름
- priority : 키워드 매칭 개수가 같을 때 우선할 질환 (숫자가 작을수록 우선)
- keywords : 환자 대화(STT)에서 찾을 의학용어/증상 표현
- api_keys : 중증질환 수용가능정보 API의 필드명 (하나라도 "Y"면 수용 가능)
             collector가 이 값으로 병원별 disease_status를 만듭니다.
"""
import json
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent / "disease_rules.json"


def _norm(s: str) -> str:
    """STT 띄어쓰기 차이를 무시하기 위해 공백 제거."""
    return s.replace(" ", "")


def load_rules(path: Path = RULES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    diseases = data["diseases"]
    seen = {}  # 키워드 -> 처음 등록한 질환 코드
    for code, rule in diseases.items():
        rule.setdefault("name", code)
        rule.setdefault("priority", 99)
        rule.setdefault("keywords", [])
        rule.setdefault("api_keys", [])

        if not rule["api_keys"]:
            print(f"⚠️ [Rules] '{code}': api_keys가 비어 있어 병원 수용 여부를 판정할 수 없습니다.")
        for kw in rule["keywords"]:
            key = _norm(kw)
            if key in seen and seen[key] != code:
                print(f"⚠️ [Rules] 키워드 '{kw}'가 '{seen[key]}'와 '{code}'에 중복 등록되어 있습니다.")
            seen.setdefault(key, code)

    return {"unknown_code": data.get("unknown_code", "unknown"), "diseases": diseases}


_RULES = load_rules()
UNKNOWN_CODE = _RULES["unknown_code"]
DISEASE_RULES = _RULES["diseases"]

MAPPING_RULES_TEXT = [
    "1. 환자 대화(STT 텍스트)에서 각 질환의 키워드가 몇 개 포함됐는지 센다. (띄어쓰기는 무시)",
    "2. 키워드가 가장 많이 매칭된 질환 코드를 선택한다.",
    "3. 매칭 개수가 같으면 priority 숫자가 작은 질환을 선택한다.",
    f"4. 매칭되는 키워드가 하나도 없으면 '{UNKNOWN_CODE}'로 분류하고, 질환 수용 여부 필터 없이 병상 여부만 본다.",
    "5. 병원 수용 가능 조건 = 응급실 병상 > 0 AND (질환 수용 가능 'Y' 또는 unknown).",
    "6. 후보는 항상 거리 오름차순으로 정렬하고, 그중 가장 가까운 1곳을 최우선 추천한다.",
]


def disease_api_keys() -> dict:
    """collector용: {질환코드: [API 필드명, ...]}"""
    return {code: rule["api_keys"] for code, rule in DISEASE_RULES.items()}


def classify_disease(text: str) -> dict:
    """대화 텍스트를 질환 코드로 분류하고, 근거(매칭 키워드)도 함께 반환."""
    norm_text = _norm(text)
    hits_by_code = {}
    for code, rule in DISEASE_RULES.items():
        hits = [kw for kw in rule["keywords"] if _norm(kw) in norm_text]
        if hits:
            hits_by_code[code] = hits

    if not hits_by_code:
        return {"code": UNKNOWN_CODE, "name": "미분류", "matched_keywords": []}

    best = max(
        hits_by_code,
        key=lambda c: (len(hits_by_code[c]), -DISEASE_RULES[c]["priority"]),
    )
    return {
        "code": best,
        "name": DISEASE_RULES[best]["name"],
        "matched_keywords": hits_by_code[best],
    }


def analyze_text_to_disease_code(text: str) -> str:
    return classify_disease(text)["code"]


def build_mapping_table() -> list:
    return [
        {
            "disease_code": code,
            "disease_name": rule["name"],
            "api_keys": rule["api_keys"],
            "priority": rule["priority"],
            "keywords": rule["keywords"],
        }
        for code, rule in sorted(DISEASE_RULES.items(), key=lambda x: x[1]["priority"])
    ]


def print_disease_mapping():
    """콘솔에 질환코드 매핑표와 규칙을 출력."""
    print("=" * 60)
    print("[질환 코드 매핑표]")
    for row in build_mapping_table():
        print(f"  - {row['disease_code']:<8} | {row['disease_name']} | 우선순위 {row['priority']}")
        print(f"      키워드: {', '.join(row['keywords'])}")
        print(f"      API 필드: {', '.join(row['api_keys']) or '(없음)'}")
    print("[매핑 규칙]")
    for rule in MAPPING_RULES_TEXT:
        print(f"  {rule}")
    print("=" * 60)