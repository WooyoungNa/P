# 포켓몬 데이터 위키

PokeAPI 공식 CSV 전체셋을 로컬 SQLite로 변환해, **한글 포켓몬 위키를 빠르게 검색/조회**하는 앱입니다.

## 성능 설계
- 첫 실행 시 CSV를 받아 `data/pokewiki.db`를 생성합니다.
- 이후 검색/상세 조회는 모두 로컬 DB에서 수행됩니다.
- 한글 이름 검색은 인덱스(`korean_name`)를 사용합니다.

## 표시 정보
- 한글 포켓몬명 검색
- 이미지
- 종족값 분포 (HP/공격/방어/특수공격/특수방어/스피드 + 총합)
- 특성/숨특 + 특성 설명
- 타입 상성 (약점/반감/무효, 타입 컬러 코딩)
- 알기술 표 (타입/분류/위력/명중/PP/효과)

## 실행 (원클릭)
### Windows
`run_pokewiki.bat` 더블 클릭

### macOS/Linux
```bash
./run_pokewiki.sh
```

## 데이터 출처
- https://pokeapi.co/
- https://github.com/PokeAPI/pokeapi/tree/master/data/v2/csv
