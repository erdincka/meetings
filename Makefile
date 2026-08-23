# Agentic Meetings -- developer entrypoints.
# Every target is idempotent and safe to re-run.

SHELL          := /bin/bash
CLUSTER        ?= meetings
KCTX           ?= kind-$(CLUSTER)
KUBECTL        ?= kubectl --context $(KCTX)
HELM           ?= helm --kube-context $(KCTX)
NODE_IMAGE     ?= kindest-node-runsc:v1.36.1
AGENT_SANDBOX_VER ?= v0.5.6
CALICO_VER     ?= v3.31.1

.DEFAULT_GOAL := help
.PHONY: help node-image kind-up kind-down bootstrap smoke smoke-gvisor smoke-sandbox \
        smoke-pgvector smoke-netpol deploy images lint test check security migrate seed demo status

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

node-image: ## Build the kind node image with gVisor (runsc) baked in
	docker build --platform linux/arm64 -t $(NODE_IMAGE) deploy/kind/node-image

kind-up: node-image ## Create the local cluster and bootstrap the platform
	@kind get clusters | grep -qx $(CLUSTER) \
	  || kind create cluster --config deploy/kind/cluster.yaml --wait 180s
	@$(MAKE) bootstrap
	@$(MAKE) smoke

kind-down: ## Delete the local cluster
	kind delete cluster --name $(CLUSTER)

bootstrap: ## Install platform prerequisites (idempotent)
	# Calico, because kindnetd does not implement NetworkPolicy -- policies
	# apply cleanly and are never enforced. Gate 3 proves the difference.
	$(KUBECTL) apply --server-side -f https://raw.githubusercontent.com/projectcalico/calico/$(CALICO_VER)/manifests/calico.yaml
	$(KUBECTL) -n kube-system rollout status daemonset/calico-node --timeout=8m
	$(HELM) repo add jetstack https://charts.jetstack.io --force-update
	$(HELM) repo add cnpg https://cloudnative-pg.github.io/charts --force-update
	$(HELM) upgrade --install cert-manager jetstack/cert-manager \
	  -n cert-manager --create-namespace --set crds.enabled=true --wait --timeout 8m
	$(HELM) upgrade --install cnpg cnpg/cloudnative-pg \
	  -n cnpg-system --create-namespace --wait --timeout 8m
	# Envoy Gateway ships the Gateway API CRDs itself (crds.enabled=true).
	# Do NOT also apply gateway-api standard-install.yaml: the two collide on
	# field-manager ownership and the Helm install fails with a CRD conflict.
	$(HELM) upgrade --install eg oci://docker.io/envoyproxy/gateway-helm \
	  -n envoy-gateway-system --create-namespace --wait --timeout 8m
	$(KUBECTL) apply -f deploy/bootstrap/gatewayclass.yaml
	$(KUBECTL) create namespace meetings --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) apply --server-side \
	  -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/$(AGENT_SANDBOX_VER)/sandbox-with-extensions.yaml
	$(KUBECTL) -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=5m
	# Only the release namespace: Helm needs it before it can store a release.
	# The sandbox namespaces are owned by the chart, which also labels them for
	# restricted Pod Security -- creating them here as well makes the chart fail
	# to adopt them.
	$(KUBECTL) create namespace meetings --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) apply -f deploy/bootstrap/cnpg-cluster.yaml
	$(KUBECTL) -n meetings wait --for=condition=Ready cluster/meetings-postgres --timeout=8m

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

images: ## Build app images and load them into the kind cluster
	bash sandbox/runtime/sync-shared.sh
	docker build --target runtime -t meetings-backend:latest backend
	docker build --target runtime -t meetings-frontend:latest frontend
	docker build --target runtime -t meetings-persona-runtime:latest sandbox/runtime
	docker build -t meetings-exec-python:latest sandbox/exec-python
	kind load docker-image meetings-backend:latest meetings-frontend:latest \
	  meetings-persona-runtime:latest meetings-exec-python:latest --name $(CLUSTER)

deploy: ## Install/upgrade the app (migrations run as a pre-upgrade hook)
	$(HELM) upgrade --install meetings deploy/charts/meetings -n meetings \
	  -f deploy/charts/meetings/values-local.yaml --wait --timeout 6m

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
	@for f in values.yaml values-local.yaml; do \
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
