{{/* The release's name for its objects, kept within Kubernetes' 63 characters. */}}
{{- define "rrotor.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "rrotor.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "rrotor.labels" -}}
{{ include "rrotor.selectorLabels" . }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "rrotor.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- .Values.serviceAccount.name | default (include "rrotor.fullname" .) -}}
{{- else -}}
{{- .Values.serviceAccount.name | default "default" -}}
{{- end -}}
{{- end -}}

{{- define "rrotor.secretName" -}}
{{- .Values.existingSecret | default (include "rrotor.fullname" .) -}}
{{- end -}}

{{/* The size presets: CPU request and limit, memory, and the disk for /data. */}}
{{- define "rrotor.preset" -}}
{{- $presets := dict
  "small" (dict "cpuRequest" "250m" "cpuLimit" "1" "memory" "1Gi" "disk" "3Gi")
  "medium" (dict "cpuRequest" "1" "cpuLimit" "2" "memory" "4Gi" "disk" "10Gi")
  "large" (dict "cpuRequest" "2" "cpuLimit" "2" "memory" "8Gi" "disk" "20Gi") -}}
{{- $p := get $presets .Values.size -}}
{{- if not $p -}}
{{- fail (printf "size must be small, medium or large, not %q" .Values.size) -}}
{{- end -}}
{{- toJson $p -}}
{{- end -}}

{{- define "rrotor.resources" -}}
{{- if .Values.resources -}}
{{- toYaml .Values.resources -}}
{{- else -}}
{{- $p := include "rrotor.preset" . | fromJson -}}
requests:
  cpu: {{ $p.cpuRequest | quote }}
  memory: {{ $p.memory }}
limits:
  cpu: {{ $p.cpuLimit | quote }}
  memory: {{ $p.memory }}
{{- end -}}
{{- end -}}

{{- define "rrotor.diskSize" -}}
{{- .Values.persistence.size | default (get (include "rrotor.preset" . | fromJson) "disk") -}}
{{- end -}}

{{- define "rrotor.publicHost" -}}
{{- required "publicHost is required: the exact host rrotor answers at, e.g. rrotor.acme.com" .Values.publicHost -}}
{{- end -}}
