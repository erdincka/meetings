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
