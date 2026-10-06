# 크롤러 서버 배포

서버 터미널에서 명령 한 번으로 `main` 코드 업데이트, 패키지 설치, 수집 기록 테이블 준비, API 서비스 재시작과 상태 확인을 수행합니다.

기본 설정은 저장소의 `hanseo-crawler.service`를 따릅니다.

- 소스 경로: `/opt/hanseo-mate/crawler/source`
- 서비스: `hanseo-crawler`
- 실행 계정: systemd 서비스의 `User` (기본 `hsmate`)
- 브랜치: `main`
- 상태 확인: `http://127.0.0.1:8000/health`

## 최초 사용

로컬에서 배포 스크립트를 포함한 변경을 커밋하고 GitHub의 `main`에 푸시해야 합니다. 서버에는 이미 Git 저장소와 systemd 서비스가 설치되어 있어야 합니다. 서버에서 GitHub에 접속할 수 있어야 하며, 비공개 저장소라면 서비스 실행 계정의 Git 인증도 필요합니다.

서버 터미널에서 먼저 실제 서비스 설정을 확인합니다.

```bash
sudo systemctl cat hanseo-crawler
```

`WorkingDirectory`가 위 기본 경로와 같고 실행 계정이 `hsmate`라면, 처음 한 번은 다음 명령으로 스크립트를 받습니다.

```bash
cd /opt/hanseo-mate/crawler/source
sudo -u hsmate git status --short
```

수정 파일이 출력되면 서버 설정이나 작업을 먼저 보존해야 합니다. 출력이 없고 서버 브랜치가 `main`이면 진행합니다.

```bash
sudo -u hsmate git pull --ff-only origin main
sudo bash scripts/deploy.sh
```

## 이후 배포

로컬 변경이 GitHub의 `main`에 올라간 뒤, 서버에서 다음 명령만 입력합니다.

```bash
sudo bash /opt/hanseo-mate/crawler/source/scripts/deploy.sh
```

성공하면 `Deployment complete`, `Service: active; health: ok`가 출력됩니다. 이는 API 기동 확인이며 실제 식단 수집, DB 저장, 스프링의 스케줄 실행까지 검증한 결과는 아닙니다.

스크립트는 서버의 수정 파일, 다른 브랜치, 원격과 다른 커밋이 있으면 중단합니다. `reset`, `clean`, 강제 브랜치 전환으로 서버 설정을 덮어쓰지 않습니다. 실패 시 자동으로 코드를 롤백하지 않습니다.

## 오류 확인

```bash
sudo journalctl -u hanseo-crawler -n 80 --no-pager
```

기본 경로와 서비스 이름이 다르면 실제 값에 맞춰 아래 환경 변수를 지정할 수 있습니다.

```bash
sudo CRAWLER_SOURCE_DIR=/actual/source/path CRAWLER_SERVICE_NAME=actual-service bash /actual/source/path/scripts/deploy.sh
```

## DB 변경과 스프링 배포

이 스크립트는 재시작 전에 `python -m crawler.cafeteria.migrate`를 실행해 `cafeteria_crawl_progress` 테이블을 없을 때만 생성하고 필요한 컬럼을 확인합니다. 재실행할 수 있으며 기존 식단 테이블이나 데이터를 삭제하지 않습니다. 크롤러 DB 계정에 테이블 생성 권한이 필요하며 준비에 실패하면 재시작 전 중단합니다. 이후 다른 테이블이나 컬럼을 변경하는 배포는 해당 배포의 별도 SQL 절차를 따라야 합니다.

`GET /cafeteria-crawl/daily-status`로 재시작 후에도 당일 성공 기록을 확인할 수 있습니다. `/health`는 API 기동 확인 용도이며 DB 접근까지 보증하지 않습니다.

월~금 01시 수집과 03~17시 재시도 일정은 스프링 변경도 배포해야 적용됩니다. 크롤러 배포만으로 호출 일정이 바뀌지 않습니다.

systemd 서비스 파일 자체를 변경했다면 `/etc/systemd/system/hanseo-crawler.service`에 변경을 반영하고 `sudo systemctl daemon-reload`를 실행한 뒤 배포합니다. 파이썬 소스만 바꾼 경우에는 이 과정이 필요하지 않습니다.

## 로컬 테스트

개발용 Python 환경에서 아래 명령으로 SQLite 기반 수집/상태/API 테스트를 실행합니다. 학교 사이트나 운영 DB에는 접속하지 않습니다.

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```
