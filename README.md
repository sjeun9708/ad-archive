# 경쟁사 광고 소재 아카이브 (뷰티 광고 연구소)

경쟁 뷰티 브랜드의 광고 이미지(공식몰 배너, 인스타그램 게시물, 리테일 기획전 배너, 상세페이지)를
**매주 월요일 오전 9시에 자동으로 수집**하고, Claude가 **후킹 요소와 디자인을 분석**해서
사이트에 자동으로 쌓아주는 프로젝트예요.

```
매주 월요일 자동 실행 (GitHub Actions)
  → 공식몰·리테일 기획전 배너, 상세페이지 자동 수집 (Playwright)
  → 인스타그램 공식 API로 브랜드 게시물 수집
  → Claude가 후킹 유형·카피·레이아웃·톤·상세페이지 구조 분석
  → data/items.json 업데이트 → GitHub Pages 자동 반영
```

---

## 처음 한 번만 하는 설정 (약 15분)

### 1. 파일 넣기

받은 폴더 안의 두 가지를 `beauty-trend` 레포에 그대로 올려주세요.

- `ad-archive/` 폴더 전체 → 레포 맨 위에
- `.github/workflows/ad-archive.yml` → 레포의 같은 위치에

올리고 나면 바로 `https://sjeun9708.github.io/beauty-trend/ad-archive/` 에서
예시 데이터로 된 화면을 볼 수 있어요.

### 2. Claude API 키 등록 (필수)

1. https://console.anthropic.com 에서 로그인 → **API Keys** → 새 키 만들기
2. 결제 수단 등록 후 소액 충전 (분석 비용은 사용한 만큼 나가요. 요금은 Anthropic 요금 페이지에서 확인)
3. GitHub 레포 → **Settings → Secrets and variables → Actions → New repository secret**
   - 이름: `ANTHROPIC_API_KEY`
   - 값: 복사한 키

### 3. 수집할 주소 채우기

`ad-archive/config.yaml`을 열어서 `""`로 비어 있는 곳에 주소를 넣어주세요.

- `official_pages`: 각 브랜드 공식몰 메인 주소, '이벤트' 메뉴 주소
- `pdp_pages`: 꼭 보고 싶은 제품 상세페이지 주소 (선택)
- `instagram`: 브랜드 인스타 아이디 (선택, 아래 5번 설정 필요)

주소는 PC 크롬에서 해당 페이지를 열고 맨 위 주소창을 복사하면 돼요.
비어 있는 항목은 자동으로 건너뛰니까, 아는 것만 채워도 괜찮아요.
올리브영 기획전과 랭킹 상위 상세페이지는 이미 설정돼 있어요.

### 4. 첫 실행

레포 → **Actions** 탭 → 왼쪽 **광고 소재 자동 수집** → **Run workflow**

10~30분 정도 걸리고, 끝나면 사이트에 실제 소재가 올라와요.
그다음부터는 매주 월요일에 알아서 돌아가요.

### 5. 인스타그램 연결 (선택)

인스타 공식 API는 **내 계정이 비즈니스 또는 크리에이터 계정**이어야 쓸 수 있어요.

1. 인스타 앱에서 프로페셔널 계정(비즈니스/크리에이터)으로 전환하고 페이스북 페이지와 연결
2. https://developers.facebook.com 에서 앱 만들기 → Instagram Graph API 추가
3. 그래프 API 탐색기에서 Meta 개발자 문서의 'Business Discovery' 안내에 나온 권한을 선택해 토큰 발급 → 장기 토큰으로 교환
4. 내 인스타 비즈니스 계정 ID 확인
5. GitHub Secrets에 `IG_USER_ID`, `IG_ACCESS_TOKEN` 등록

장기 토큰은 약 60일마다 만료되니, 수집 로그에 인스타 오류가 보이면 토큰을 다시 발급해 주세요.
가져올 수 있는 건 상대 브랜드도 비즈니스/크리에이터 계정인 경우에 한해요.

---

## 실행 결과 확인하기

Actions 탭에서 실행 기록을 누르면 어떤 페이지에서 몇 개를 가져왔는지 한국어 로그로 보여요.

| 로그 메시지 | 의미와 해결 |
|---|---|
| `robots.txt에서 막힌 주소라 건너뜀` | 사이트가 자동 수집을 허용하지 않는 페이지예요. 그 주소는 빼주세요. |
| `페이지를 열지 못함` | 사이트가 해외 서버 접속을 막았거나 느린 경우예요. 다음 주에 다시 시도돼요. |
| `'기획전' 메뉴를 찾지 못함` | 사이트 메뉴 이름이 바뀌었어요. config의 `promo_menu_text`를 실제 메뉴 이름으로 바꿔주세요. |
| `인스타 API 오류` | 토큰 만료 또는 상대 계정이 비즈니스 계정이 아닌 경우예요. |
| `분석 실패` | 일시적인 API 오류예요. 분석 안 된 소재는 다음 실행 때 다시 분석돼요. |

GitHub Actions는 미국 서버에서 돌아가서, 일부 한국 사이트는 접속을 막거나 다른 화면을 보여줄 수 있어요.
첫 실행 로그를 보고 잘 안 되는 사이트가 있으면 주소를 바꾸거나 빼면 돼요.

---

## 폴더 구조

```
ad-archive/
├─ index.html              사이트 (아카이브 / 상세페이지 분석 / 인사이트)
├─ config.yaml             브랜드, 주소, 수집 옵션
├─ data/items.json         수집·분석 결과 (자동 생성)
├─ data/insights.json      AI가 쓴 최근 4주 인사이트 (자동 생성)
├─ images/                 축소 저장된 소재 이미지 (자동 생성)
└─ pipeline/
   ├─ run.py               전체 실행
   ├─ collect_web.py       공식몰·리테일·상세페이지 수집
   ├─ collect_instagram.py 인스타 수집
   └─ analyze.py           Claude 분석
```

내 컴퓨터에서 직접 돌려보고 싶다면:

```bash
pip install -r ad-archive/requirements.txt
python -m playwright install chromium
export ANTHROPIC_API_KEY=키값
python ad-archive/pipeline/run.py
```

---

## 수집 원칙

- 각 사이트의 robots.txt를 확인하고 허용된 페이지만 가져와요.
- 페이지 사이에 몇 초씩 쉬고, 주 1회만 실행해서 사이트에 부담을 주지 않아요.
- 로그인이 필요한 곳, 유료 광고 지면(네이버·카카오 배너 등)은 수집하지 않아요.
- 사이트에는 축소한 이미지만 올리고, 출처와 수집일을 함께 표기해요.
  모든 광고 소재의 저작권은 각 브랜드에 있어요.
