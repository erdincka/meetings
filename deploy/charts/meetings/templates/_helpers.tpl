{{- define "meetings.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "meetings.labels" -}}
app.kubernetes.io/name: {{ include "meetings.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "meetings.image" -}}
{{- if .registry }}{{ .registry }}/{{ .image }}{{ else }}{{ .image }}{{ end -}}
{{- end -}}

{{/*
Pod-level hardening applied to every workload in the chart. The namespaces are
labelled pod-security.kubernetes.io/enforce=restricted, so these are required
rather than optional.
*/}}
{{- define "meetings.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 1001
runAsGroup: 1001
fsGroup: 1001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "meetings.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/*
The OTLP endpoint actually in force: empty unless observability is enabled.

Resolved in one place because three templates consume it -- the backend's
ConfigMap, every sandbox's env, and the sandbox NetworkPolicy egress rule that
lets the export leave the pod. Deciding it independently in each is how a
sandbox ends up told to export traces to a collector its own policy forbids.
*/}}
{{- define "meetings.otlpEndpoint" -}}
{{- if .Values.observability.enabled }}{{ .Values.observability.otlpEndpoint }}{{ end -}}
{{- end -}}

{{/*
The Sandbox Router for one tier, as its callers address it.
*/}}
{{- define "meetings.routerUrl" -}}
http://{{ include "meetings.name" .root }}-sandbox-router-{{ .tier }}.{{ .root.Release.Namespace }}.svc:8080
{{- end -}}

{{/*
A NetworkPolicy peer selecting one tier's router pods.
*/}}
{{- define "meetings.routerPeer" -}}
- namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: {{ .root.Release.Namespace }}
  podSelector:
    matchLabels:
      app.kubernetes.io/name: {{ include "meetings.name" .root }}
      app.kubernetes.io/component: sandbox-router
      meetings/router-tier: {{ .tier }}
{{- end -}}

{{- define "meetings.routerCommonIngress" -}}
# Kubelet probes arrive at the pod address, from the node.
- ports:
    - {protocol: TCP, port: 8081}
{{- if .Values.observability.enabled }}
- from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: {{ .Values.observability.namespace }}
  ports:
    - {protocol: TCP, port: 9090}
{{- end }}
{{- end -}}

{{- define "meetings.routerCommonEgress" -}}
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
  ports:
    - {protocol: UDP, port: 53}
    - {protocol: TCP, port: 53}
# The apiserver, for the pod cache. Pinned like the sandboxes' own route.
{{- $cidrs := .Values.sandbox.apiserverCIDRs | default list }}
- to:
    {{- if $cidrs }}
    {{- range $cidrs }}
    - ipBlock:
        cidr: {{ . | quote }}
    {{- end }}
    {{- else }}
    - ipBlock:
        cidr: 0.0.0.0/0
    {{- end }}
  ports:
    - {protocol: TCP, port: 443}
    - {protocol: TCP, port: 6443}
{{- with (include "meetings.otlpEndpoint" .) }}
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: {{ $.Values.observability.namespace }}
  ports:
    - {protocol: TCP, port: {{ regexFind ":[0-9]+$" . | trimPrefix ":" | default "4317" }}}
{{- end }}
{{- end -}}
