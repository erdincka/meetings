# Agentic Meetings -- developer and operator entrypoints.
# Every target is idempotent and safe to re-run.

SHELL          := /bin/bash
# Homebrew and Rancher Desktop shims are not on PATH in a non-login shell, so a
# recipe can silently fall back to "command not found" -- which chart-validate
# then reports as a clean run over zero resources.
export PATH := $(HOME)/.rd/bin:/opt/homebrew/bin:$(PATH)

# Cluster-specific values live in deploy/cluster/cluster.env, which is
# gitignored. Copy the example and edit it before anything else.
CLUSTER_ENV    := deploy/cluster/cluster.env
KCTX           ?=
KUBECTL        := kubectl$(if $(KCTX), --context $(KCTX),)
HELM           := helm$(if $(KCTX), --kube-context $(KCTX),)

REGISTRY       ?= $(shell sed -n 's/^IMAGE_REGISTRY=//p' $(CLUSTER_ENV) 2>/dev/null)
BUILDER        ?= $(shell sed -n 's/^BUILDER=//p' $(CLUSTER_ENV) 2>/dev/null)
BUILDER_DIR    ?= $(shell sed -n 's/^BUILDER_DIR=//p' $(CLUSTER_ENV) 2>/dev/null)

TEMPLATES := deploy/cluster/templates/metallb-pool.yaml.tmpl \
             deploy/cluster/templates/registry.yaml.tmpl \
             deploy/cluster/templates/gatewayclass.yaml.tmpl \
             deploy/charts/meetings/values-cluster.yaml.tmpl

.DEFAULT_GOAL := help
.PHONY: help preflight prerequisites smoke smoke-gvisor smoke-sandbox smoke-netpol \
        smoke-pgvector status render images deploy deploy-observed seed demo \
        operator-token verify-images observability observability-down \
        lint test test-integration test-frontend chart-validate check migrate-check security

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- cluster

preflight: ## Check that your cluster meets the requirements
	@bash deploy/cluster/preflight.sh

prerequisites: ## Install missing platform components (see docs/requirements.md)
	@bash deploy/cluster/install-prerequisites.sh all

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

render: ## Render templates from deploy/cluster/cluster.env
	@python3 scripts/render.py $(TEMPLATES)

verify-images: ## Prove the published images came from this repo's CI, unaltered
	@bash scripts/verify-images.sh

images: render ## Build the five images and push them to your registry
	bash sandbox/runtime/sync-shared.sh
	@bash scripts/build-images.sh

deploy: render ## Install/upgrade the app (migrations run as a pre-upgrade hook)
	@bash scripts/runtime-secret.sh
	@set -e; eval "$$(bash scripts/image-tags.sh)"; \
	  $(HELM) upgrade --install meetings deploy/charts/meetings -n meetings --create-namespace \
	    -f deploy/charts/meetings/values-cluster.yaml \
	    --set backend.image=$$BACKEND_TAG \
	    --set frontend.image=$$FRONTEND_TAG \
	    --set sandbox.runtimeImage=$$RUNTIME_TAG \
	    --set sandbox.execImage=$$EXEC_TAG \
	    --set corpus.image=$$CORPUS_TAG \
	    --wait --timeout 8m

# Kept so the command in older notes still works, but it is now the same deploy.
# Observability used to be turned on by these two --set flags, which meant a
# later plain `make deploy` silently turned it back off: the ServiceMonitor was
# deleted, tracing stopped, and every Grafana panel read "no data" with nothing
# in an error state anywhere. It is OBSERVABILITY_ENABLED in cluster.env now, so
# the two commands cannot disagree about it.
deploy-observed: deploy ## Deprecated alias for `deploy` (see OBSERVABILITY_ENABLED)
	@echo
	@echo "note: deploy-observed is now identical to deploy."
	@echo "      Tracing and scraping follow OBSERVABILITY_ENABLED in deploy/cluster/cluster.env."

seed: ## Load reference personas, documents and templates
	$(KUBECTL) -n meetings exec deploy/meetings-backend -- \
	  python -c "import asyncio; from scripts.seed import seed_data; asyncio.run(seed_data())"

operator-token: ## Print the operator and viewer tokens for this deployment
	@echo -n "operator: "; $(KUBECTL) -n meetings get secret meetings-auth \
	  -o jsonpath='{.data.OPERATOR_TOKEN}' | base64 -d; echo
	@echo -n "viewer:   "; $(KUBECTL) -n meetings get secret meetings-auth \
	  -o jsonpath='{.data.VIEWER_TOKEN}' | base64 -d; echo

demo: ## Run the least-privilege demo meeting to completion (in-cluster)
	@# The driver runs from the same image as the backend it drives, so the two
	@# are never a different build.
	@# export, not a bare assignment: the renderer below is a separate process
	@# and reads this from the environment, so without it `make demo` dies on
	@# KeyError: 'BACKEND_IMAGE' before it ever reaches the cluster.
	@export BACKEND_IMAGE=$$($(KUBECTL) -n meetings get deploy meetings-backend \
	  -o jsonpath='{.spec.template.spec.containers[0].image}'); \
	  python3 -c "import os; from pathlib import Path; \
	  s=Path('deploy/demo/run-demo.py').read_text(); \
	  i='\n'.join('    '+l if l.strip() else '' for l in s.splitlines()); \
	  t=Path('deploy/demo/demo-job.yaml').read_text(); \
	  t=t.replace('{{SCRIPT}}', i).replace('{{BACKEND_IMAGE}}', os.environ['BACKEND_IMAGE']); \
	  Path('deploy/demo/demo-job.rendered.yaml').write_text(t)"
	$(KUBECTL) delete job meetings-demo -n meetings --ignore-not-found
	$(KUBECTL) apply -f deploy/demo/demo-job.rendered.yaml
	@# `kubectl wait` fails outright on a selector that matches nothing yet
	@# ("no matching resources found") rather than waiting for it to appear, and
	@# the Job's pod does not exist the instant after apply. So wait for the pod
	@# to exist first, then for it to be ready.
	@for i in $$(seq 1 30); do \
	  [ -n "$$($(KUBECTL) -n meetings get pod -l job-name=meetings-demo -o name 2>/dev/null)" ] && break; \
	  sleep 2; \
	done
	$(KUBECTL) -n meetings wait --for=condition=Ready pod -l job-name=meetings-demo --timeout=120s
	$(KUBECTL) -n meetings logs -f job/meetings-demo

observability: ## Install Prometheus, Grafana and Tempo (optional, ~1.5GB)
	@bash deploy/bootstrap/40-observability.sh install

observability-down: ## Remove the observability stack
	@bash deploy/bootstrap/40-observability.sh remove

# ---------------------------------------------------------------- quality

lint: ## ruff + format + mypy + eslint + tsc + helm/kubeconform
	cd backend && uv run ruff check app scripts tests
	cd backend && uv run ruff format --check app scripts tests alembic
	cd backend && uv run mypy app scripts
	cd sandbox/runtime && uv run ruff check runtime tests
	cd sandbox/runtime && uv run mypy runtime
	cd frontend && npm run lint
	cd frontend && npx tsc --noEmit
	bash sandbox/runtime/sync-shared.sh && git diff --exit-code sandbox/runtime/runtime/protocol.py sandbox/runtime/runtime/recovery.py sandbox/runtime/runtime/prompts.py sandbox/runtime/runtime/tool_guidance.py
	bash scripts/generate-profile-values.sh && git diff --exit-code deploy/charts/meetings/values.yaml
	$(MAKE) chart-validate

test: ## Unit tests: backend, sandbox runtime, frontend
	cd backend && uv run pytest tests/unit -v --cov=app --cov-report=term-missing
	cd sandbox/runtime && uv run pytest tests -v
	cd frontend && npm test

test-integration: ## Backend integration tests (needs Postgres; see the target's comment)
	@# Skipped with a reason if no database is reachable, so this stays runnable
	@# on a laptop. To provide one:
	@#   docker run --rm -d --name meetings-test-pg -p 5432:5432 \
	@#     -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test \
	@#     pgvector/pgvector:pg17
	cd backend && uv run pytest tests/integration -v

test-frontend: ## Frontend tests only
	cd frontend && npm test

chart-validate: ## Render every values profile and validate against API schemas
	@command -v helm >/dev/null || { echo "helm not found on PATH"; exit 1; }
	@command -v kubeconform >/dev/null || { echo "kubeconform not found on PATH"; exit 1; }
	@set -e; for f in values.yaml values-cluster.yaml; do \
	  echo ">> $$f"; \
	  helm template meetings deploy/charts/meetings -n meetings \
	    -f deploy/charts/meetings/$$f \
	  | kubeconform -strict -summary -kubernetes-version 1.36.1 \
	      -schema-location default \
	      -schema-location 'deploy/schemas/{{ .ResourceKind }}-{{ .Group }}-{{ .ResourceAPIVersion }}.json' \
	      -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'; \
	done

check: lint test test-integration security ## Everything CI runs

migrate-check: ## Fail if the ORM has drifted from the migrations
	cd backend && uv run alembic check

security: ## Secret and vulnerability scan, same as CI
	gitleaks detect --source . --redact --no-banner --exit-code 1
	docker run --rm -v "$$PWD:/src" -w /src aquasec/trivy:latest fs \
	  --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 \
	  --scanners vuln,secret,misconfig --no-progress .
