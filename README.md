# 포켓몬 데이터 위키

PokeAPI 공식 CSV 데이터셋을 로컬 SQLite로 구성해, **한글 검색 + 빠른 조회**가 가능한 포켓몬 위키 앱입니다.

## 핵심 아이디어 (검색/로딩 최적화)
- 앱 첫 실행 시 PokeAPI GitHub의 CSV를 한 번에 받아서 로컬 DB(`data/pokewiki.db`)를 생성합니다.
- 검색은 로컬 SQLite 인덱스(`korean_name`)로 처리하여 빠르게 응답합니다.
- 포켓몬 상세(종족값/특성/타입상성/알기술)도 모두 로컬 DB 조회로 처리해 네트워크 지연을 줄입니다.
- 이미지도 포켓몬 ID 기반 공식 artwork URL 규칙으로 즉시 로딩합니다.

## 실행 (One Click)
### Windows
`run_pokewiki.bat` 더블클릭

### macOS/Linux
```bash
./run_pokewiki.sh
```

서버가 `http://127.0.0.1:7860`에서 열리고, Windows에서는 Edge 자동 실행을 시도합니다.

## 기능
- 한글 포켓몬 이름 검색
- 이미지
- 종족값 분포
- 특성 + 숨겨진 특성
- 타입 상성 기준 약점/반감/무효
- 알기술

## 데이터 출처
- https://pokeapi.co/
- https://github.com/PokeAPI/pokeapi/tree/master/data/v2/csv
