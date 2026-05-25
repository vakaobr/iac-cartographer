{{/*
Expand the name of the chart.
*/}}
{{- define "iac-cartographer.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name. Used as the prefix on every resource so multiple
releases of the same chart in the same namespace don't collide.
*/}}
{{- define "iac-cartographer.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart label — `chart-version`.
*/}}
{{- define "iac-cartographer.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels — applied to every resource the chart creates.
Includes the standard `app.kubernetes.io/*` set plus any user-provided
`commonLabels` from values.yaml.
*/}}
{{- define "iac-cartographer.labels" -}}
helm.sh/chart: {{ include "iac-cartographer.chart" . }}
{{ include "iac-cartographer.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Selector labels — subset of `labels` that is stable across upgrades
(no version / chart-version), so PodSpec selectors don't drift.
*/}}
{{- define "iac-cartographer.selectorLabels" -}}
app.kubernetes.io/name: {{ include "iac-cartographer.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Pick the ServiceAccount name. Honour `.Values.serviceAccount.name` when set,
otherwise derive from the fullname when create=true, otherwise fall back
to `default` so the pod still has a working SA reference.
*/}}
{{- define "iac-cartographer.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "iac-cartographer.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
ConfigMap name — defaults to the chart-managed one, but defers to a
caller-provided `config.existingConfigMap` when set.
*/}}
{{- define "iac-cartographer.configMapName" -}}
{{- if .Values.config.existingConfigMap -}}
{{- .Values.config.existingConfigMap -}}
{{- else -}}
{{- include "iac-cartographer.fullname" . -}}-config
{{- end -}}
{{- end -}}

{{/*
Secret name — same logic as the ConfigMap helper.
*/}}
{{- define "iac-cartographer.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- include "iac-cartographer.fullname" . -}}-secrets
{{- end -}}
{{- end -}}
