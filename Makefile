# Agentic Meetings -- developer entrypoints.
# Every target is idempotent and safe to re-run.

SHELL          := /bin/bash
# Rancher Desktop and Homebrew shims are not on PATH in a non-login shell, so a
# recipe would silently fall back to "command not found" -- which chart-validate
# then reported as a clean run over zero resources.
export PATH := $(HOME)/.rd/bin:/opt/homebrew/bin:$(PATH)
KCTX           ?= k3s-lab
BUILDER        ?= ubuntu@10.1.1.33
REGISTRY       ?= 10.1.1.240:5000
# Source is synced here and built natively: the laptop is arm64 and the nodes
# are x86_64, and cross-building Python/Node images under emulation is slow
# enough to hurt the inner loop.
BUILDER_DIR    ?= /home/ubuntu/meetings
KUBECTL        ?= kubectl --context $(KCTX)
HELM           ?= helm --kube-context $(KCTX)
AGENT_SANDBOX_VER ?= v0.5.6

.DEFAULT_GOAL := help
.PHONY: help node-image kind-up kind-down bootstrap smoke smoke-gvisor smoke-sandbox \
        smoke-pgvector smoke-netpol deploy deploy-observed images lint test check \
        security migrate seed demo observability observability-down status

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

cluster-up: ## Provision the VMs, install k3s and the platform (idempotent)
	bash deploy/k3s/bootstrap.sh all
	@$(MAKE) smoke

cluster-down: ## Stop the lab VMs (does not destroy them)
	@ssh -o BatchMode=yes root@10.1.1.3 'for id in 1030 1031 1032 1033; do qm stop $$id 2>/dev/null || true; done'
	@echo "lab VMs stopped"

bootstrap: ## Install/refresh the platform components only
	bash deploy/k3s/bootstrap.sh platform

smoke: smoke-gvisor smoke-netpol smoke-sandbox ## Run every fail-fast gate

smoke-gvisor: ## GATE 1: assert sandboxes really run under gVisor, not runc
	@echo ">> gate 1: gVisor runtime"
	@$(KUBECTL) delete job gvisor-smoke --ignore-not-found >/dev/null 2>&1 || true
	@$(KUBECTL) apply -f deploy/bootstrap/smoke-gvisor.yaml >/dev/null
	@$(KUBECTL) wait --for=condition=complete job/gvisor-smoke --timeout=180s \
	  || { echo "FAIL: gVisor gate"; $(KUBECTL) logs job/gvisor-smoke; exit 1; }
	@$(KUBECTL) logs job/gvisor-smoke
	@$(KUBECTL) delete job gvisor-smoke --ignore-not-found >/dev/null

smoke-netpol: ## GATE 3: assert NetworkPolicy is enforced, not merely accepted
	@echo ">> gate 3: NetworkPolicy enforcement"
	@bash deploy/bootstrap/smoke-netpol.sh $(KCTX)

smoke-sandbox: ## GATE 2: assert the Agent Sandbox control plane works end to end
	@echo ">> gate 2: Agent Sandbox round trip"
	@bash deploy/bootstrap/smoke-sandbox.sh $(KCTX)

smoke-pgvector: ## Assert pgvector is really loaded via ImageVolume
	@$(KUBECTL) -n meetings exec meetings-postgres-1 -c postgres -- \
	  psql -U postgres -d meetings -tAc \
	  "SELECT '[1,2,3]'::vector <-> '[4,5,6]'::vector;"

status: ## Show cluster state at a glance
	@$(KUBECTL) get nodes -o wide
	@$(KUBECTL) get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded
	@$(KUBECTL) -n meetings get cluster,pods

# ---------------------------------------------------------------- app

images: ## Build images on the builder and push to the registry
	bash sandbox/runtime/sync-shared.sh
	rsync -az --delete \
	  --exclude '.git' --exclude '**/node_modules' --exclude '**/.venv' \
	  --exclude '**/__pycache__' --exclude '.local-assets' --exclude '**/.next' \
	  ./ $(BUILDER):$(BUILDER_DIR)/
	@ssh -o BatchMode=yes $(BUILDER) 'cd $(BUILDER_DIR) && \
	  R=$(REGISTRY); \
	  docker build -q --target runtime -t $$R/meetings-backend:latest backend && \
	  docker build -q --target runtime -t $$R/meetings-frontend:latest frontend && \
	  docker build -q --target runtime -t $$R/meetings-persona-runtime:latest sandbox/runtime && \
	  docker build -q -t $$R/meetings-exec-python:latest sandbox/exec-python && \
	  docker build -q -t $$R/meetings-corpus:latest sandbox/corpus' >/dev/null
	@bash scripts/image-tags.sh --push

deploy: ## Install/upgrade the app (migrations run as a pre-upgrade hook)
	@set -e; eval "$$(bash scripts/image-tags.sh)"; \
	  $(HELM) upgrade --install meetings deploy/charts/meetings -n meetings \
	    -f deploy/charts/meetings/values-lab.yaml \
	    --set backend.image=$$BACKEND_TAG \
	    --set frontend.image=$$FRONTEND_TAG \
	    --set sandbox.runtimeImage=$$RUNTIME_TAG \
	    --set sandbox.execImage=$$EXEC_TAG \
	    --set corpus.image=$$CORPUS_TAG \
	    --wait --timeout 8m

demo: ## Run the least-privilege demo meeting to completion (in-cluster)
	@python3 -c "from pathlib import Path; \
	  s=Path('deploy/demo/run-demo.py').read_text(); \
	  i='\n'.join('    '+l if l.strip() else '' for l in s.splitlines()); \
	  t=Path('deploy/demo/demo-job.yaml').read_text(); \
	  Path('deploy/demo/demo-job.rendered.yaml').write_text(t.replace('{{SCRIPT}}', i))"
	$(KUBECTL) delete job meetings-demo -n meetings --ignore-not-found
	$(KUBECTL) apply -f deploy/demo/demo-job.rendered.yaml
	$(KUBECTL) -n meetings wait --for=condition=Ready pod -l job-name=meetings-demo --timeout=120s
	$(KUBECTL) -n meetings logs -f job/meetings-demo

observability: ## Install Prometheus, Grafana and Tempo (optional, ~1.5GB)
	@bash deploy/bootstrap/40-observability.sh install

observability-down: ## Remove the observability stack
	@bash deploy/bootstrap/40-observability.sh remove

deploy-observed: ## Deploy with tracing and scraping enabled
	@set -e; eval "$$(bash scripts/image-tags.sh)"; \
	  $(HELM) upgrade --install meetings deploy/charts/meetings -n meetings \
	    -f deploy/charts/meetings/values-lab.yaml \
	    --set backend.image=$$BACKEND_TAG \
	    --set frontend.image=$$FRONTEND_TAG \
	    --set sandbox.runtimeImage=$$RUNTIME_TAG \
	    --set sandbox.execImage=$$EXEC_TAG \
	    --set corpus.image=$$CORPUS_TAG \
	    --set observability.serviceMonitor=true \
	    --set observability.otlpEndpoint=http://tempo.observability.svc:4317 \
	    --wait --timeout 8m

seed: ## Load reference personas, documents and templates
	$(KUBECTL) -n meetings exec deploy/meetings-backend -- \
	  python -c "import asyncio; from scripts.seed import seed_data; asyncio.run(seed_data())"

# ---------------------------------------------------------------- quality

lint: ## ruff + format + mypy + eslint + tsc + helm/kubeconform
	cd backend && uv run ruff check app scripts tests
	cd backend && uv run ruff format --check app scripts tests alembic
	cd backend && uv run mypy app scripts
	cd sandbox/runtime && uv run ruff check runtime tests
	cd sandbox/runtime && uv run mypy runtime
	cd frontend && npm run lint
	cd frontend && npx tsc --noEmit
	bash sandbox/runtime/sync-shared.sh && git diff --exit-code sandbox/runtime/runtime/protocol.py sandbox/runtime/runtime/recovery.py
	bash scripts/generate-profile-values.sh && git diff --exit-code deploy/charts/meetings/values.yaml
	$(MAKE) chart-validate

test: ## Backend + sandbox runtime tests with coverage
	cd backend && uv run pytest tests/unit -v --cov=app --cov-report=term-missing
	cd sandbox/runtime && uv run pytest tests -v

chart-validate: ## Render every values profile and validate against API schemas
	@command -v helm >/dev/null || { echo "helm not found on PATH"; exit 1; }
	@command -v kubeconform >/dev/null || { echo "kubeconform not found on PATH"; exit 1; }
	@set -e; for f in values.yaml values-lab.yaml; do \
	  echo ">> $$f"; \
	  helm template meetings deploy/charts/meetings -n meetings \
	    -f deploy/charts/meetings/$$f \
	  | kubeconform -strict -summary -kubernetes-version 1.36.1 \
	      -schema-location default \
	      -schema-location 'deploy/schemas/{{ .ResourceKind }}-{{ .Group }}-{{ .ResourceAPIVersion }}.json' \
	      -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'; \
	done

check: lint test security ## Everything CI runs

migrate-check: ## Fail if the ORM has drifted from the migrations
	cd backend && uv run alembic check

security: ## Secret and vulnerability scan, same as CI
	gitleaks detect --source . --redact --no-banner --exit-code 1
	docker run --rm -v "$$PWD:/src" -w /src aquasec/trivy:latest fs \
	  --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 \
	  --scanners vuln,secret,misconfig --no-progress .
