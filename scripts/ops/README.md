# ops — 호스트 운영 스크립트

- `pv-cloud-deploy.sh` — 클라우드 호스트 자동배포 (타이머 3분 + 즉시 트리거)
- `pv-cloud-deploy.service` / `.timer` — systemd 유닛

설치:
```bash
sudo install -m 755 scripts/ops/pv-cloud-deploy.sh /usr/local/bin/
sudo install -m 644 scripts/ops/pv-cloud-deploy.{service,timer} /etc/systemd/system/
sudo mkdir -p /var/lib/pv-cloud
sudo systemctl daemon-reload && sudo systemctl enable --now pv-cloud-deploy.timer
```

일시정지: `touch /home/ubuntu/peter-voice/.deploy-pause` (해제는 rm)
로그: `/var/log/pv-cloud-deploy.log`

## deploy.env (선택)
```bash
sudo tee /etc/pv-cloud/deploy.env >/dev/null <<'ENV'
PV_DEPLOY_BRANCH=main       # 전용 호스트는 stable 로 분리 가능
PV_API_KEY=...              # 실패 알림용 (없으면 로그만)
ENV
sudo chown ubuntu:ubuntu /etc/pv-cloud/deploy.env   # 서비스가 User=ubuntu 라 필수
sudo chmod 600 /etc/pv-cloud/deploy.env
```
