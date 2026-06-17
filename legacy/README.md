# Legacy Archive

기본 FastAPI 런타임에서 더 이상 직접 사용하지 않는 코드와 참고 자산을 이 디렉터리로 격리했습니다.

- `apps/`: 이전 Streamlit 진입점
- `reference_html/`: Stitch 원본 참고 HTML
- `static/`: 초기 정적 프로토타입 HTML
- `tools/`: `emails.json` import, 레거시 스키마 마이그레이션, 호환 래퍼

현재 기본 실행 경로는 루트의 `app.py`, `templates/`, `static/index.html`, `pipeline/*.py`입니다.
