"""
질환 코드 매핑 규칙 로더 / 분류기.

팀원이 질환 코드나 주증상 범주 매핑을 추가할 때는 이 파일이 아니라
disease_rules.json만 수정하면 됩니다. (수정 후 서버 재시작 / Lambda는 zip 다시 빌드)

JSON 항목
- category_map : A 파트 일지의 주증상 범주(category, cc_category.py의 23개) -> 질환 코드.
                 'unknown'이거나 여기에 없는 범주는 질환 필터 없이 병상 여부만 확인합니다.
- category_labels : 범주의 한글 표시 이름 (화면의 chiefComplaint에 사용)
- diseases     : 질환 코드별 설정
    - name     : 질환 표시 이름
    - conditions : 화면에 보여줄 질환명 목록 (mappedConditions / accepts에 사용, 없으면 [name])
    - priority : 표시 순서 (숫자가 작을수록 앞)
    - api_keys : 중증질환 수용가능정보 API의 필드명 (하나라도 "Y"면 수용 가능)
                 collector / Lambda가 이 값으로 병원별 disease_status를 만듭니다.
    - keywords : (선택) STT 원문에서 찾을 의학용어. 범주 입력 방식에서는 쓰지 않고,
                 classify_disease(text)를 직접 호출할 때만 사용됩니다.
"""
import json
import re
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent / "disease_rules.json"


def _norm(s: str) -> str:
    """STT 띄어쓰기 차이를 무시하기 위해 공백 제거."""
    return s.replace(" ", "")


def _norm_category(category: str) -> str:
    """'Chest_Pain', 'chest  pain ' 같은 표기 차이를 같은 값으로 맞춤."""
    return re.sub(r"[\s_\-]+", " ", str(category)).strip().lower()


def load_rules(path: Path = RULES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    diseases = data["diseases"]
    seen = {}  # 키워드 -> 처음 등록한 질환 코드
    for code, rule in diseases.items():
        rule.setdefault("name", code)
        rule.setdefault("conditions", [rule["name"]])
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

    unknown_code = data.get("unknown_code", "unknown")
    category_map = {}
    for category, code in data.get("category_map", {}).items():
        if code != unknown_code and code not in diseases:
            print(f"⚠️ [Rules] category_map의 '{category}' -> '{code}': diseases에 없는 질환 코드라 unknown으로 처리합니다.")
            code = unknown_code
        category_map[_norm_category(category)] = code

    category_labels = {_norm_category(k): v for k, v in data.get("category_labels", {}).items()}

    return {
        "unknown_code": unknown_code,
        "diseases": diseases,
        "category_map": category_map,
        "category_labels": category_labels,
    }


_RULES = load_rules()
UNKNOWN_CODE = _RULES["unknown_code"]
DISEASE_RULES = _RULES["diseases"]
CATEGORY_MAP = _RULES["category_map"]
CATEGORY_LABELS = _RULES["category_labels"]

MAPPING_RULES_TEXT = [
    "1. 입력된 주증상 범주(category)를 소문자·공백 기준으로 정리한 뒤 category_map에서 질환 코드를 찾는다.",
    f"2. 값이 '{UNKNOWN_CODE}'이거나 category_map에 없는 범주는 질환 수용 여부 필터 없이 병상 여부만 본다.",
    "3. 병원 수용 가능 조건 = 응급실 병상 > 0 AND (질환 수용 가능 'Y' 또는 unknown).",
    "4. 후보는 항상 거리 오름차순으로 정렬하고, 그중 가장 가까운 1곳을 최우선 추천한다.",
    "5. 반경은 5 -> 15 -> 30 -> 50km 순으로, 현재 반경에 후보가 없으면 다음 단계로 넓힌다.",
]


def disease_api_keys() -> dict:
    """collector / Lambda용: {질환코드: [API 필드명, ...]}"""
    return {code: rule["api_keys"] for code, rule in DISEASE_RULES.items()}


def classify_category(category: str) -> dict:
    """
    주증상 범주를 질환 코드로 변환.
    category_known=False면 category_map에 없는 이름(오타/신규 범주)이라는 뜻이고, unknown으로 처리됩니다.
    """
    key = _norm_category(category)
    code = CATEGORY_MAP.get(key, UNKNOWN_CODE)
    name = "미분류" if code == UNKNOWN_CODE else DISEASE_RULES[code]["name"]
    return {
        "code": code,
        "name": name,
        "category_known": key in CATEGORY_MAP,
        "label": CATEGORY_LABELS.get(key, str(category).strip()),   # 한글 표시 이름(없으면 입력 그대로)
        "conditions": [] if code == UNKNOWN_CODE else list(DISEASE_RULES[code]["conditions"]),
    }


def classify_disease(text: str) -> dict:
    """(선택) STT 원문에서 키워드로 질환 분류. 범주 입력 방식에서는 사용하지 않음."""
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


def build_mapping_table() -> list:
    return [
        {
            "disease_code": code,
            "disease_name": rule["name"],
            "api_keys": rule["api_keys"],
            "priority": rule["priority"],
        }
        for code, rule in sorted(DISEASE_RULES.items(), key=lambda x: x[1]["priority"])
    ]


def build_category_table() -> list:
    """주증상 범주 -> 질환 코드 매핑표."""
    return [
        {
            "category": category,
            "disease_code": code,
            "disease_name": "미분류" if code == UNKNOWN_CODE else DISEASE_RULES[code]["name"],
        }
        for category, code in CATEGORY_MAP.items()
    ]


def print_disease_mapping():
    """콘솔에 질환코드/범주 매핑표와 규칙을 출력."""
    print("=" * 60)
    print("[질환 코드 표]")
    for row in build_mapping_table():
        print(f"  - {row['disease_code']:<8} | {row['disease_name']} | API 필드: {', '.join(row['api_keys']) or '(없음)'}")
    print("[주증상 범주 -> 질환 코드 매핑표]")
    for row in build_category_table():
        print(f"  - {row['category']:<22} -> {row['disease_code']} ({row['disease_name']})")
    print("  (표에 없는 범주도 unknown으로 처리)")
    print("[매핑 규칙]")
    for rule in MAPPING_RULES_TEXT:
        print(f"  {rule}")
    print("=" * 60)