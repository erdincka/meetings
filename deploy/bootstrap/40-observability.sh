#!/usr/bin/env bash
# Prometheus, Grafana and Tempo for the local cluster.
#
# Separate from `make bootstrap` and off by default. The stack costs roughly
# 1.5GB, which on a 12GB VM competes directly with the sandbox warm pools it is
# meant to be observing -- and the app runs perfectly well without it, because
# tracing is a no-op when no OTLP endpoint is configured.
#
#   make observability      install
#   make observability-down remove
set -euo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

CTX="${KCTX:-kind-meetings}"
NS=observability
ACTION="${1:-install}"

h() { helm --kube-context "$CTX" "$@"; }
k() { kubectl --context "$CTX" "$@"; }

if [[ "$ACTION" == "remove" ]]; then
  h uninstall kube-prometheus-stack -n "$NS" || true
  h uninstall tempo -n "$NS" || true
  k delete namespace "$NS" --ignore-not-found
  echo "observability stack removed"
  exit 0
fi

h repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
h repo add grafana https://grafana.github.io/helm-charts --force-update >/dev/null

k create namespace "$NS" --dry-run=client -o yaml | k apply -f - >/dev/null

# Sized down hard: default retention and replica counts assume a real cluster.
h upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n "$NS" \
  --set prometheus.prometheusSpec.retention=6h \
  --set prometheus.prometheusSpec.resources.requests.memory=400Mi \
  --set prometheus.prometheusSpec.resources.limits.memory=1Gi \
  --set 'prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false' \
  --set alertmanager.enabled=false \
  --set grafana.adminPassword=admin \
  --set grafana.resources.requests.memory=128Mi \
  --set grafana.resources.limits.memory=384Mi \
  --wait --timeout 12m

h upgrade --install tempo grafana/tempo -n "$NS" \
  --set tempo.retention=6h \
  --set tempo.resources.requests.memory=128Mi \
  --set tempo.resources.limits.memory=512Mi \
  --wait --timeout 8m

echo
echo "Grafana:  kubectl -n $NS port-forward svc/kube-prometheus-stack-grafana 3000:80"
echo "          then http://localhost:3000  (admin / admin)"
echo "Dashboard: import docs/dashboards/agentic-meetings.json"
echo
echo "Then redeploy the app with tracing and scraping enabled:"
echo "  make deploy-observed"
