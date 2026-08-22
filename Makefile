# Agentic Meetings -- developer entrypoints.
# Every target is idempotent and safe to re-run.

SHELL          := /bin/bash
CLUSTER        ?= meetings
KCTX           ?= kind-$(CLUSTER)
KUBECTL        ?= kubectl --context $(KCTX)
HELM           ?= helm --kube-context $(KCTX)
NODE_IMAGE     ?= kindest-node-runsc:v1.36.1
AGENT_SANDBOX_VER ?= v0.5.6

.DEFAULT_GOAL := help
.PHONY: help node-image kind-up kind-down bootstrap smoke smoke-gvisor smoke-sandbox deploy lint test e2e status

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
	$(KUBECTL) apply --server-side \
	  -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/$(AGENT_SANDBOX_VER)/sandbox-with-extensions.yaml
	$(KUBECTL) -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=5m
	$(KUBECTL) create namespace meetings --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) create namespace meetings-sandboxes --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) create namespace meetings-exec --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) label namespace meetings-sandboxes meetings-exec \
	  pod-security.kubernetes.io/enforce=restricted --overwrite
	$(KUBECTL) apply -f deploy/bootstrap/cnpg-cluster.yaml
	$(KUBECTL) -n meetings wait --for=condition=Ready cluster/meetings-postgres --timeout=8m

smoke: smoke-gvisor smoke-sandbox ## Run both fail-fast gates

smoke-gvisor: ## GATE 1: assert sandboxes really run under gVisor, not runc
	@echo ">> gate 1: gVisor runtime"
	@$(KUBECTL) delete job gvisor-smoke --ignore-not-found >/dev/null 2>&1 || true
	@$(KUBECTL) apply -f deploy/bootstrap/smoke-gvisor.yaml >/dev/null
	@$(KUBECTL) wait --for=condition=complete job/gvisor-smoke --timeout=180s \
	  || { echo "FAIL: gVisor gate"; $(KUBECTL) logs job/gvisor-smoke; exit 1; }
	@$(KUBECTL) logs job/gvisor-smoke
	@$(KUBECTL) delete job gvisor-smoke --ignore-not-found >/dev/null

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
