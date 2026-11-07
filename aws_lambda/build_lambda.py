"""
Lambda 배포용 zip 생성.  실행:  python aws_lambda/build_lambda.py
  -> aws_lambda/hc2-c-match.zip  (lambda_function.py + 프로젝트 루트의 rules/ 폴더)
rules/는 프로젝트 루트의 것을 그대로 담으므로, disease_rules.json을 고친 뒤 다시 실행하면 됩니다.
"""
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "hc2-c-match.zip"


def main():
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HERE / "lambda_function.py", "lambda_function.py")
        for f in sorted((ROOT / "rules").iterdir()):
            if f.suffix in (".py", ".json"):
                z.write(f, f"rules/{f.name}")
        names = z.namelist()
    print(f"생성 완료: {OUT}")
    for n in names:
        print("  -", n)


if __name__ == "__main__":
    main()