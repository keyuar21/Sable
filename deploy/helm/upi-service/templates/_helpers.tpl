{{/* Chart name, overridable. */}}
{{- define "upi-service.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Resource name.

Each microservice is its own release of this chart, so the release name alone
is the right resource name: release "upi-payer-bank" gives a Service called
upi-payer-bank, which is what the other services' peer URLs point at. Appending
the chart name would make every address wrong.
*/}}
{{- define "upi-service.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "upi-service.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "upi-service.labels" -}}
helm.sh/chart: {{ include "upi-service.chart" . }}
{{ include "upi-service.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: upi-simulator
{{- with .Values.service.name }}
upi.sim/service: {{ . | quote }}
{{- end }}
{{- end -}}

{{- define "upi-service.selectorLabels" -}}
app.kubernetes.io/name: {{ include "upi-service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "upi-service.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "upi-service.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
The ConfigMap key AND the mounted filename: "<service.name>.yaml" unless
app.configName overrides it. This is the per-service file the container reads
at runtime.
*/}}
{{- define "upi-service.configName" -}}
{{- if .Values.app.configName -}}
{{- .Values.app.configName -}}
{{- else -}}
{{- printf "%s.yaml" (required "service.name is required (it names the runtime config file)" .Values.service.name) -}}
{{- end -}}
{{- end -}}

{{- define "upi-service.configMapName" -}}
{{- printf "%s-config" (include "upi-service.fullname" .) -}}
{{- end -}}

{{- define "upi-service.secretName" -}}
{{- if .Values.existingSecret -}}
{{- .Values.existingSecret -}}
{{- else -}}
{{- printf "%s-secret" (include "upi-service.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "upi-service.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
