# 배포 가이드 — 완전 무료 (도메인·서버 없음)

구조:
- **GitHub** : 코드 + 데이터(screener.db) 보관 + 매일 자동 수집(Actions)
- **Streamlit Community Cloud** : 무료 호스팅, 공개 주소(`*.streamlit.app`)

비용 $0. 로그인 없음 — 주소를 아는 사람은 누구나 접속. cloudflared·VM·도메인 전부 불필요.

```
[GitHub 저장소]  ──코드+DB──▶  [Streamlit Cloud]  ──공개 주소──▶  사람들
      ▲                                (앱이 DB를 읽어 화면 표시)
      │ 매일 22:00 DB 갱신 커밋
[GitHub Actions cron] ──FnGuide API──▶ 컨센서스 수집
```

---

## 1. GitHub 저장소 만들기

1. github.com 가입/로그인
2. **New repository** → 이름 예: `consensus-screener` → **Private** 선택 → Create
3. 로컬 프로젝트를 이 저장소로 올린다 (아래 명령을 프로젝트 폴더에서):

```bash
cd C:/Users/user/Desktop/consensus-screener
git init
git add .
git commit -m "init: 컨센서스 스크리너"
git branch -M main
git remote add origin https://github.com/<내계정>/consensus-screener.git
git push -u origin main
```

> `.env`(API 키)는 `.gitignore`에 있어 **올라가지 않는다**. 데이터 `data/screener.db`는
> 일부러 올라간다(Community Cloud가 읽어야 하므로).

## 2. API 키를 GitHub Secrets에 등록

매일 수집(Actions)이 FnGuide를 호출하려면 키가 필요하다. 코드가 아니라 Secrets에 숨긴다.

1. 저장소 → **Settings → Secrets and variables → Actions → New repository secret**
2. Name: `FNSPACE_API_KEY`  /  Value: (FnGuide API 키)
3. Add secret

## 3. Streamlit Community Cloud에 앱 올리기

1. share.streamlit.io 접속 → **GitHub로 로그인**
2. **Create app → Deploy a public app from a repo... (또는 private 저장소 인증)**
3. 저장소 `consensus-screener`, 브랜치 `main`, 파일 `app.py` 선택
4. **Deploy** → 몇 분 뒤 `https://<이름>.streamlit.app` 주소 생성
5. 이 주소를 볼 사람들에게 공유

> 앱은 API 키가 필요 없다(저장된 DB만 읽음). 그래서 Community Cloud에는 키를 안 넣어도 된다.

## 4. 매일 자동 수집 확인

`.github/workflows/daily_update.yml` 이 이미 저장소에 있으므로, 올리는 즉시 스케줄이 등록된다.

- 자동: **평일 22:00(한국시간)** 마다 컨센서스 갱신 → DB 커밋 → 앱 자동 갱신
- 수동 테스트: 저장소 **Actions 탭 → "매일 컨센서스 갱신" → Run workflow**
- 실행 로그도 Actions 탭에서 확인

## 5. 커버 종목 명단 주기적 갱신 (선택)

신규 커버 개시 종목을 잡으려면 가끔 `discover_universe.py`를 돌려야 한다.
지금은 수동으로 로컬에서 실행하거나, 나중에 별도 workflow로 추가한다.

---

## 알아둘 점

- **로그인 없음**: 주소를 아는 사람은 누구나 본다. 민감하면 주소를 아무한테나 뿌리지 말 것.
- **DB가 저장소에 쌓임**: 매일 커밋되어 git 용량이 조금씩 는다(연 ~1GB 수준). 1~2년 뒤
  느려지면 히스토리를 한 번 정리(재초기화)하면 된다 — 담당에게 요청.
- **코인**: 매일 수집은 월 ~3,100코인(6%). FnSpace 이용통계에서 잔량 확인.
- **FnGuide 계약**: 이 방식은 데이터를 '공개'하는 것에 가깝다. 유료 데이터 외부 공개가
  계약상 문제 없는지 한 번 확인해둘 것.
