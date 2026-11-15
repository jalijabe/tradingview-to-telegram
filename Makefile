PY ?= .venv/bin/python
RUN := PYTHONPATH=src:. $(PY)
PC_ON_MARK := 2026-11-05T00:00:00Z=provisioned concurrency on

.PHONY: venv lint test check check-all mock graphs docs-graphs tf-fmt tf-validate tf-test ash-install ash clean

venv:
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q -r requirements-dev.txt

lint:
	$(PY) -m ruff check .

test:
	$(PY) -m pytest --cov --cov-report=term-missing --cov-fail-under=95

## Mock data -> graphs in out/mock-graphs (same tooling works on real exports, see tools/export_logs.py)
mock:
	$(RUN) -m tools.mockdata --out out/mock --days 6 --seed 7

graphs: mock
	$(RUN) -m tools.graphs out/mock/delivered.jsonl out/mock/received.jsonl --out out/mock-graphs --mark "$(PC_ON_MARK)"

## Regenerate the committed sample graphs shown in README / docs
docs-graphs: mock
	$(RUN) -m tools.graphs out/mock/delivered.jsonl out/mock/received.jsonl --out docs/img/mock --mark "$(PC_ON_MARK)"

tf-fmt:
	terraform fmt -recursive infra

TF_ROOTS := infra/envs/dev infra/envs/prod infra/bootstrap infra/modules/pipeline infra/modules/observer_ec2
TF_TESTED := infra/modules/pipeline infra/modules/observer_ec2 infra/envs/prod

tf-validate:
	terraform fmt -check -recursive infra
	@for d in $(TF_ROOTS); do \
		echo "== $$d"; \
		terraform -chdir=$$d init -backend=false -input=false >/dev/null && terraform -chdir=$$d validate || exit 1; \
	done

## terraform test: plans against mock AWS providers (no credentials needed)
tf-test: tf-validate
	@for d in $(TF_TESTED); do echo "== $$d"; terraform -chdir=$$d test || exit 1; done

check: lint test tf-test

## Everything CI runs, including the (slower) security scan
check-all: check ash

## ASH (AWS Automated Security Helper). Installed OUTSIDE the repo on purpose: ASH refuses to run scanner binaries
## that live inside the tree it scans. Same targets in CI (.github/workflows/ash.yml). Config: .ash/.ash.yaml
ASH_VERSION := v3.8.0
ASH_HOME ?= $(HOME)/.ash
ASH_VENV := $(ASH_HOME)/venv-$(ASH_VERSION)
ASH_PATH := PATH="$(ASH_VENV)/bin:$(ASH_HOME)/bin:$$PATH"
# Scanners used by this repo (cfn-nag / cdk-nag are disabled in .ash/.ash.yaml: CloudFormation/CDK only)
ASH_TOOLS := actionlint bandit cfn-guard cfn-lint checkov gitleaks grype opengrep semgrep syft trivy zizmor

ash-install:
	@test -x "$(ASH_VENV)/bin/ash" || { \
		python3 -m venv "$(ASH_VENV)" && \
		"$(ASH_VENV)/bin/pip" install -q --upgrade pip && \
		"$(ASH_VENV)/bin/pip" install -q uv \
			'automated-security-helper @ git+https://github.com/awslabs/automated-security-helper.git@$(ASH_VERSION)'; }
	$(ASH_PATH) ash dependencies install $(addprefix --tool ,$(ASH_TOOLS))

ash:
	$(ASH_PATH) ash scan --mode local --source-dir . --no-progress --fail-on-findings --fail-on-incomplete-scanners

clean:
	rm -rf out export .pytest_cache .coverage htmlcov infra/**/.build .ash/ash_output
