Two entry points are invoked directly with the venv Python:
- `venv/bin/python infra/metrics.py [--prometheus|--runtime|--reset]` prints human-readable stats, Prometheus text, or resets in-memory counters + audit log.
- `venv/bin/python infra/metrics_server.py [--port N]` starts a background HTTP server on 0.0.0.0:9464 scraping the same `memory.db`.
Alert channels are configured purely via environment variables: `SLACK_WEBHOOK_URL`, `SMTP_HOST/PORT/USER/PASS`, `ALERT_FROM/TO`, `PUSHOVER_USER_KEY/PUSHOVER_API_TOKEN`.