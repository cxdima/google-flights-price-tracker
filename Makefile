.PHONY: deploy test test-unit test-integration help

deploy:
	@bash scripts/deploy.sh

test:
	@.venv/bin/python -m pytest tests/test_tracker.py tests/test_notifier.py tests/test_auth.py -v

test-integration:
	@python tests/test_local.py

help:
	@echo "make deploy           Build, push, and deploy to AWS Lambda"
	@echo "make test             Run unit tests (no browser required)"
	@echo "make test-integration Run full end-to-end test with live Chrome"
