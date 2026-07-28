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
