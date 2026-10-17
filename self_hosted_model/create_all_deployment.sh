kind create cluster --name ai-lab --wait 120s
kubectl config use-context kind-ai-lab
kubectl get nodes

mkdir selfhosted_ai
cd ~/selfhosted_ai



cat > namespace.yaml <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: ai-inference
  labels:
    app.kubernetes.io/part-of: selfhosted-ai
EOF

cat > configmap.yaml <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: vllm-config
  namespace: ai-inference
data:
  MODEL_NAME: "Qwen/Qwen2.5-0.5B-Instruct"
  SERVED_MODEL_NAME: "payment-assistant"
  MAX_MODEL_LEN: "8192"
EOF

cat > secret.yaml.example <<'EOF'
# Template only. Never apply this file directly.
apiVersion: v1
kind: Secret
metadata:
  name: vllm-api
  namespace: ai-inference
type: Opaque
stringData:
  api-key: "REPLACE_WITH_A_RANDOM_SECRET"
EOF

cat > deployment.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-inference
  namespace: ai-inference
  labels:
    app: vllm-inference
spec:
  # Start with one working CPU inference replica.
  replicas: 2

  selector:
    matchLabels:
      app: vllm-inference

  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 0
      maxUnavailable: 1

  template:
    metadata:
      labels:
        app: vllm-inference
    spec:
      terminationGracePeriodSeconds: 60

      containers:
        - name: vllm
          image: vllm/vllm-openai-cpu:latest-x86_64
          imagePullPolicy: IfNotPresent

          # The CPU image was tested with this launch configuration.
          command: ["vllm"]
          args:
            - serve
            - $(MODEL_NAME)
            - --host
            - "0.0.0.0"
            - --port
            - "8000"
            - --served-model-name
            - $(SERVED_MODEL_NAME)
            - --dtype
            - bfloat16
            - --max-model-len
            - $(MAX_MODEL_LEN)
            - --api-key
            - $(VLLM_API_KEY)

          env:
            - name: MODEL_NAME
              valueFrom:
                configMapKeyRef:
                  name: vllm-config
                  key: MODEL_NAME

            - name: SERVED_MODEL_NAME
              valueFrom:
                configMapKeyRef:
                  name: vllm-config
                  key: SERVED_MODEL_NAME

            - name: MAX_MODEL_LEN
              valueFrom:
                configMapKeyRef:
                  name: vllm-config
                  key: MAX_MODEL_LEN

            - name: VLLM_API_KEY
              valueFrom:
                secretKeyRef:
                  name: vllm-api
                  key: api-key

            - name: VLLM_CPU_KVCACHE_SPACE
              value: "2"

            - name: HF_HOME
              value: /tmp/huggingface

            - name: OMP_NUM_THREADS
              value: "2"

          ports:
            - name: http
              containerPort: 8000

          resources:
            requests:
              cpu: "1200m"
              memory: 6Gi
            limits:
              cpu: "2"
              memory: 9Gi

          # CPU model loading and compilation can take several minutes.
          startupProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 10
            failureThreshold: 90

          readinessProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3

          livenessProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 20
            timeoutSeconds: 5
            failureThreshold: 3

          volumeMounts:
            - name: shared-memory
              mountPath: /dev/shm

      volumes:
        - name: shared-memory
          emptyDir:
            medium: Memory
            sizeLimit: 2Gi
EOF

cat > service.yaml <<'EOF'
apiVersion: v1
kind: Service
metadata:
  name: vllm-inference
  namespace: ai-inference
spec:
  type: ClusterIP
  selector:
    app: vllm-inference
  ports:
    - name: http
      port: 8000
      targetPort: http
EOF

cat > hpa.yaml <<'EOF'
# Optional: do not apply until metrics-server is installed
# and resource usage has been validated.
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: vllm-inference
  namespace: ai-inference
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: vllm-inference
  minReplicas: 1
  maxReplicas: 2
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 75
EOF

cat > kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - configmap.yaml
  - deployment.yaml
  - service.yaml
EOF

cat > README.md <<'EOF'
# Self-hosted AI inference lab

## Environment
- Lightning AI CPU Studio
- kind Kubernetes cluster: ai-lab
- CPU-compatible vLLM OpenAI API image
- Initial model: Qwen2.5-0.5B-Instruct
- Initial replicas: 2

## 1. Verify Kubernetes
kubectl config current-context
kubectl get nodes

## 2. Create API key Secret
Generate a key without saving it in YAML or Git:

KEY="$(openssl rand -hex 32)"

kubectl create namespace ai-inference \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic vllm-api \
  -n ai-inference \
  --from-literal=api-key="$KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

unset KEY

## 3. Validate and deploy
kubectl kustomize .
kubectl apply -k .

kubectl rollout status deployment/vllm-inference \
  -n ai-inference --timeout=20m

kubectl get pods,svc -n ai-inference

## 4. Test the API
Terminal 1:
kubectl port-forward -n ai-inference \
  service/vllm-inference 8000:8000

Terminal 2:
KEY="$(kubectl get secret vllm-api -n ai-inference \
  -o jsonpath='{.data.api-key}' | base64 -d)"

curl -sS http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $KEY"

curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "payment-assistant",
    "messages": [
      {"role": "user", "content": "Explain a failed payment briefly."}
    ],
    "max_tokens": 80
  }'

unset KEY

## 5. Monitor resources
kubectl get pods -n ai-inference -o wide
kubectl describe deployment vllm-inference -n ai-inference
kubectl top pods -n ai-inference

## 6. Scaling
Do not scale to three replicas on this four-core,
16-GiB machine without validating resource availability.

HPA is intentionally excluded from kustomization.yaml.
CPU-based HPA is only a learning example and does not directly
measure inference queue depth or response latency.

## Security
- Keep the model Service internal.
- Do not commit API keys.
- Keep payment MCP tools read-only initially.
- Do not allow the model to execute payment actions directly.
EOF

echo "Updated files:"
ls -lh

echo "Validating rendered Kubernetes configuration..."
kubectl kustomize .

cat > deploy.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="ai-inference"

echo "==> Checking Kubernetes context"
kubectl config current-context
kubectl get nodes

echo "==> Creating namespace"
kubectl create namespace "$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "==> Creating API Secret if it does not exist"
if ! kubectl get secret vllm-api -n "$NAMESPACE" >/dev/null 2>&1; then
  KEY="$(openssl rand -hex 32)"
  kubectl create secret generic vllm-api \
    -n "$NAMESPACE" \
    --from-literal=api-key="$KEY"
  unset KEY
else
  echo "Existing vllm-api Secret found; preserving it."
fi

echo "==> Validating YAML"
kubectl kustomize .

echo "==> Deploying application"
kubectl apply -k .

echo "==> Waiting for vLLM rollout"
kubectl rollout status deployment/vllm-inference \
  -n "$NAMESPACE" --timeout=20m

echo "==> Deployment status"
kubectl get deployment,pods,svc -n "$NAMESPACE" -o wide

echo "==> Done"
echo "To test the API, run:"
echo "kubectl port-forward -n $NAMESPACE service/vllm-inference 8000:8000"
EOF

chmod +x deploy.sh
./deploy.sh


echo "Waiting for vLLM pod to be ready..."
kubectl wait --namespace ai-inference \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/name=vllm-inference \
  --timeout=300s

echo "Starting port-forward..."
kubectl port-forward -n ai-inference service/vllm-inference 8000:8000 &
PORT_FORWARD_PID=$!


sudo mkdir -p --mode=0755 /usr/share/keyrings

curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null

echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared noble main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list


sudo apt-get update
sudo apt-get install -y cloudflared


kubectl get secret vllm-api -n ai-inference \
  -o jsonpath='{.data.api-key}' | base64 -d
echo