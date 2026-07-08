.PHONY: help test lint deploy run-local logs

PYTHON ?= .venv/bin/python

help:
	@echo "make test       Run unit tests (no browser, no AWS)"
	@echo "make lint       Ruff lint over src/ and tests/"
	@echo "make deploy     Build, push, and deploy to AWS Lambda"
	@echo "make run-local  Full pipeline against live Chrome (visible window)"
	@echo "make logs       Tail the Lambda's CloudWatch logs"

test:
	@$(PYTHON) -m pytest

lint:
	@$(PYTHON) -m ruff check src tests

deploy:
	@bash scripts/deploy.sh

run-local:
	@$(PYTHON) scripts/run_local.py

logs:
	@aws logs tail /aws/lambda/$${PROJECT_NAME:-gfpricetracker} --follow --region $${AWS_REGION:-us-east-1}
