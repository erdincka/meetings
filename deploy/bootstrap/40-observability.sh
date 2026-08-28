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

# Unset means the current context, as everywhere else here. A named default
# would install into whichever cluster once carried that name, or fail against a
# context that no longer exists -- both worse than using what kubectl is pointed
# at, which is what the rest of the Makefile does.
CTX="${KCTX:-}"
NS=observability
ACTION="${1:-install}"
REPO=$(cd "$(dirname "$0")/../.." && pwd)

# The Grafana hostname is derived from APP_DOMAIN, exactly as the chart derives
# the listener that admits the route -- one setting, so the two cannot disagree.
# Read here as well as at deploy time; an explicit environment variable still
# wins, matching preflight.sh and scripts/render.py.
_env_domain="${APP_DOMAIN:-}"
ENV_FILE=$(cd "$(dirname "$0")/../cluster" && pwd)/cluster.env
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
APP_DOMAIN="${_env_domain:-${APP_DOMAIN:-}}"
OBSERVABILITY_HOST="${APP_DOMAIN:+grafana.${APP_DOMAIN}}"
# The Gateway to attach to, which the app's own chart owns.
GATEWAY_NS=${GATEWAY_NS:-meetings}
GATEWAY_NAME=${GATEWAY_NAME:-meetings}

h() { if [ -n "$CTX" ]; then helm --kube-context "$CTX" "$@"; else helm "$@"; fi; }
k() { if [ -n "$CTX" ]; then kubectl --context "$CTX" "$@"; else kubectl "$@"; fi; }

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

# Grafana derives absolute URLs -- redirects, share links, OAuth callbacks --
# from root_url, which defaults to localhost. Reached by any other name it then
# hands out links back to the browser's own machine, which fail in a way that
# looks like Grafana is broken rather than misconfigured.
GRAFANA_URL_ARGS=()
if [ -n "$OBSERVABILITY_HOST" ]; then
  GRAFANA_URL_ARGS=(
    --set "grafana.grafana\.ini.server.domain=${OBSERVABILITY_HOST}"
    --set "grafana.grafana\.ini.server.root_url=http://${OBSERVABILITY_HOST}"
  )
fi
# Expanded below as ${A[@]+"${A[@]}"}, not "${A[@]}": macOS ships bash 3.2,
# where an empty array under `set -u` is an unbound variable rather than zero
# words -- so the plain form aborts the script whenever no host is configured,
# which is the default path.

# Sized down hard: default retention and replica counts assume a real cluster.
#
# Grafana's limit is 768Mi rather than the 384Mi this started with, because
# 384Mi was not a small budget -- it was a wrong one. Idle Grafana with the two
# provisioning sidecars sits around 370Mi, so the limit was inside normal
# working set and the container was OOMKilled on ordinary use. A limit that
# close to steady state is not a tighter constraint, it is a restart loop.
h upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n "$NS" \
  --set prometheus.prometheusSpec.retention=6h \
  --set prometheus.prometheusSpec.resources.requests.memory=400Mi \
  --set prometheus.prometheusSpec.resources.limits.memory=1Gi \
  --set 'prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false' \
  --set alertmanager.enabled=false \
  --set grafana.adminPassword=admin \
  --set grafana.resources.requests.memory=256Mi \
  --set grafana.resources.limits.memory=768Mi \
  ${GRAFANA_URL_ARGS[@]+"${GRAFANA_URL_ARGS[@]}"} \
  --set-json 'grafana.additionalDataSources=[{"name":"Tempo","type":"tempo","uid":"tempo","access":"proxy","url":"http://tempo.observability.svc:3200"}]' \
  --wait --timeout 12m

h upgrade --install tempo grafana/tempo -n "$NS" \
  --set tempo.retention=6h \
  --set tempo.resources.requests.memory=128Mi \
  --set tempo.resources.limits.memory=512Mi \
  --wait --timeout 8m

# ---------------------------------------------------------------- dashboard
#
# Loaded from the repository by Grafana's sidecar, which watches for this label.
# It was previously left to a manual import step, while the documentation
# claimed the dashboard was provisioned rather than clicked together -- and a
# hand-imported dashboard drifts from the checked-in one the moment anybody
# edits a panel, which is exactly what provisioning is for.
k create configmap meetings-dashboards -n "$NS" \
  --from-file="$REPO/docs/dashboards/agentic-meetings.json" \
  --dry-run=client -o yaml | k apply -f - >/dev/null
k label configmap meetings-dashboards -n "$NS" grafana_dashboard=1 --overwrite >/dev/null

# ---------------------------------------------------------------- Gateway route
#
# Published on the app's Gateway rather than a second one, so a single address
# and a single wildcard DNS record cover both. The listener that admits this
# route is created by the app's chart from the same OBSERVABILITY_HOST, so
# `make deploy` must have run since that value was set -- without the listener
# the route stays Accepted=False with NoMatchingParent, which is reported below
# rather than left to be discovered later.
#
# The route lives here, next to the stack it points at, so it is removed by
# `make observability-down` along with everything else. The chart never creates
# resources in a namespace its release does not own.
if [ -n "$OBSERVABILITY_HOST" ]; then
  k apply -f - >/dev/null <<YAML
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: grafana
  namespace: ${NS}
spec:
  parentRefs:
    - name: ${GATEWAY_NAME}
      namespace: ${GATEWAY_NS}
      # Named explicitly: the app's own listener on this Gateway is
      # \`from: Same\` and would refuse this route anyway, but an unnamed
      # parentRef reports that refusal as a partial attach rather than as
      # the plain fact that only one listener was ever meant to take it.
      sectionName: observability
  hostnames:
    - ${OBSERVABILITY_HOST}
  rules:
    - backendRefs:
        - name: kube-prometheus-stack-grafana
          port: 80
YAML

  # A route the Gateway did not accept still exists and still looks fine to
  # `kubectl get`, so ask for the condition rather than trusting the apply.
  accepted=$(k -n "$NS" get httproute grafana \
    -o jsonpath='{.status.parents[0].conditions[?(@.type=="Accepted")].status}' 2>/dev/null)
  for _ in 1 2 3 4 5; do
    [ "$accepted" = "True" ] && break
    sleep 2
    accepted=$(k -n "$NS" get httproute grafana \
      -o jsonpath='{.status.parents[0].conditions[?(@.type=="Accepted")].status}' 2>/dev/null)
  done
  if [ "$accepted" = "True" ]; then
    echo
    echo "Grafana:  http://${OBSERVABILITY_HOST}  (admin / admin)"
  else
    echo
    echo "WARNING: the Grafana route was not accepted by ${GATEWAY_NS}/${GATEWAY_NAME}."
    echo "  The listener comes from OBSERVABILITY_HOST in deploy/cluster/cluster.env."
    echo "  Set it there and run 'make deploy', then re-run this."
    k -n "$NS" get httproute grafana \
      -o jsonpath='{.status.parents[0].conditions[*].message}{"\n"}' 2>/dev/null
  fi
else
  k -n "$NS" delete httproute grafana --ignore-not-found >/dev/null 2>&1 || true
  echo
  echo "Grafana:  kubectl -n $NS port-forward svc/kube-prometheus-stack-grafana 3000:80"
  echo "          then http://localhost:3000  (admin / admin)"
  echo "          or set OBSERVABILITY_HOST in deploy/cluster/cluster.env to"
  echo "          publish it on the app's Gateway instead."
fi
echo "Dashboard: 'Agentic Meetings', provisioned automatically"
echo
echo "Then redeploy the app with tracing and scraping enabled:"
echo "  make deploy-observed"
