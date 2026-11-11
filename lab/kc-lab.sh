#!/usr/bin/env bash
# kc-lab.sh - Kubecost Actions test lab on kind + Colima (macOS)
#
# Usage:  ./lab/kc-lab.sh <step> [step...]
# Settings: lab/lab.env (copy lab/lab.env.example). Defaults run this lab NEXT TO an existing one:
#   cluster kcresize-lab, UI on http://localhost:9091, work dir ./.lab/<cluster>, same Colima VM.
#
#   preflight  check CPU / memory / disk / port before creating anything
#   up         preflight runtime cluster metrics objectstore secrets kubecost expose workloads
#   tools      brew install anything missing (kind colima kubectl helm)
#   runtime    start Colima on macOS / Docker on Linux (re-run after reboot)
#   cluster    create/reuse kind cluster, switch context
#   metrics    install + patch metrics-server
#   objectstore  S3-compatible store (SeaweedFS) + bucket. MinIO images are no
#              longer publicly pullable (Sept 2026). Alias: minio
#   secrets    federated-store + product-key secrets (or Actions signing keys on free tier)
#   signing    (re)create the RSA key pair Actions needs on the free tier
#   kubecost   helm install Kubecost with Actions enabled
#   workloads  kcresize sample apps (lab/workloads.yaml); LEGACY_WORKLOADS=1 adds resource-hog/oversized-idle
#   status     nodes, pods, kubectl top
#   expose     always-on UI at http://localhost:$UI_PORT (NodePort, no port-forward)
#   ui         fallback: port-forward the UI to http://localhost:$UI_PORT
#   reset      delete + recreate sample workloads (Kubecost stays)
#   clean      remove sample workloads only (Kubecost + object store stay)
#   uninstall  remove Kubecost and the object store (cluster stays)
#   destroy    delete THIS kind cluster (asks first); Colima keeps running for other labs
#   share      public HTTPS URL for the API, protected by basic auth
#              (nginx proxy + Cloudflare Tunnel, both inside the cluster).
#              SHARE_HOSTNAME=kubecost.example.com -> fixed URL (needs a Cloudflare domain)
#              no SHARE_HOSTNAME                   -> random *.trycloudflare.com URL
#   unshare    stop the tunnel (proxy and credentials stay)
#   auth       (re)apply only the basic-auth proxy; creds in ./kc-lab/basic-auth.env
#
# Kubecost is persistent: only 'uninstall' or 'destroy' remove it.
# After a reboot just run: ./lab/kc-lab.sh runtime
#
# License key: export KUBECOST_LICENSE_KEY=... or put productkey.json next
# to this script ({ "key": "..." }). With neither, Kubecost runs on the free
# tier (Actions included, up to 250 cores). KUBECOST_FREE=1 forces free tier.
# Every step is safe to re-run.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
[[ -f "$LAB_DIR/lab.env" ]] && { set -a; source "$LAB_DIR/lab.env"; set +a; }

CLUSTER="${CLUSTER:-kcresize-lab}"
CTX="kind-${CLUSTER}"
COLIMA_CPU="${COLIMA_CPU:-4}"
COLIMA_MEM="${COLIMA_MEM:-16}"
COLIMA_DISK="${COLIMA_DISK:-60}"
S3_ACCESS_KEY="${S3_ACCESS_KEY:-kcadmin}"
S3_SECRET_KEY="${S3_SECRET_KEY:-kcadmin-secret-123}"
S3_NS=objectstore
S3_ENDPOINT="s3.${S3_NS}.svc.cluster.local:9000"
BUCKET="${BUCKET:-kubecost-actions}"
LICENSE_FILE="${LICENSE_FILE:-./productkey.json}"
WORKDIR="${WORKDIR:-$LAB_DIR/../.lab/$CLUSTER}"
COLIMA_PROFILE="${COLIMA_PROFILE:-default}"
LEGACY_WORKLOADS="${LEGACY_WORKLOADS:-0}"
STOP_COLIMA_ON_DESTROY="${STOP_COLIMA_ON_DESTROY:-0}"
HOG_REPLICAS="${HOG_REPLICAS:-2}"
HOG_CPU_LIMIT="${HOG_CPU_LIMIT:-1}"   # cores each resource-hog pod may burn
WITH_QUOTA="${WITH_QUOTA:-1}"
UI_PORT="${UI_PORT:-9091}"      # host port for the always-on UI (9090 is usually an existing lab)
UI_NODEPORT=30090
SHARE_HOSTNAME="${SHARE_HOSTNAME:-}"   # e.g. kubecost.example.com (zone on Cloudflare)
TUNNEL_NAME="${TUNNEL_NAME:-$CLUSTER}"
CF_DIR="$HOME/.cloudflared"
BASIC_USER="${BASIC_USER:-kubecost}"
AUTH_FILE="$WORKDIR/basic-auth.env"     # generated creds, chmod 600, do not commit
SIGN_DIR="$WORKDIR/signing"             # free-tier Actions RSA keys, do not commit
SIGN_PRIV_SECRET=kubecost-actions-signing-private
SIGN_PUB_SECRET=kubecost-actions-signing-public

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARN: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
k()    { kubectl --context "$CTX" "$@"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$WORKDIR"

step_tools() {
  log "Checking tools"
  local missing=()
  command -v kind    >/dev/null || missing+=(kind)
  if [[ "$(uname)" == Darwin ]]; then
    command -v colima >/dev/null || missing+=(colima)
  fi
  command -v kubectl >/dev/null || missing+=(kubernetes-cli)
  command -v helm    >/dev/null || missing+=(helm)
  command -v docker  >/dev/null || missing+=(docker)
  if ((${#missing[@]})); then
    command -v brew >/dev/null || die "Homebrew not found; install: ${missing[*]}"
    brew install "${missing[@]}"
  fi
  echo "All tools present."
}

step_runtime() {
  if [[ "$(uname)" == Darwin ]]; then
    log "Starting Colima"
    if colima status --profile "$COLIMA_PROFILE" >/dev/null 2>&1; then
      echo "Colima ($COLIMA_PROFILE) already running."
    else
      colima start --profile "$COLIMA_PROFILE" --cpu "$COLIMA_CPU" --memory "$COLIMA_MEM" --disk "$COLIMA_DISK"
    fi
    # kind nodes fail with "Too many open files" under the VM's default inotify limits
    colima ssh --profile "$COLIMA_PROFILE" -- sudo sh -c 'printf "fs.inotify.max_user_watches=524288\nfs.inotify.max_user_instances=512\n" > /etc/sysctl.d/99-kind.conf && sysctl --system >/dev/null' \
      || warn "Could not raise inotify limits in the Colima VM"
  else
    log "Using local Docker (Linux)"
    docker info >/dev/null 2>&1 || sudo systemctl start docker
  fi
  docker info --format 'Docker server {{.ServerVersion}} ({{.Architecture}})' \
    || die "Docker daemon not reachable"
  local node="${CLUSTER}-control-plane"
  if docker inspect "$node" >/dev/null 2>&1 &&
     [[ "$(docker inspect -f '{{.State.Running}}' "$node")" != true ]]; then
    echo "Restarting kind node $node"; docker start "$node" >/dev/null
  fi
  if docker inspect "$node" >/dev/null 2>&1; then
    kubectl --context "$CTX" wait --for=condition=Ready node --all --timeout=180s || true
    echo "Kubecost UI: http://localhost:$UI_PORT (pods may need a minute after restart)"
  fi
}

step_cluster() {
  log "Cluster $CLUSTER"
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    echo "Cluster exists, reusing."
  else
    if port_busy "$UI_PORT"; then
      die "Port $UI_PORT is already in use (another lab?). Set UI_PORT in lab/lab.env."
    fi
    cat > "$WORKDIR/kind-config.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - {containerPort: $UI_NODEPORT, hostPort: $UI_PORT, listenAddress: "127.0.0.1"}
EOF
    kind create cluster --name "$CLUSTER" --config "$WORKDIR/kind-config.yaml" --wait 120s
  fi
  kind export kubeconfig --name "$CLUSTER"
  kubectl config use-context "$CTX"
  k wait --for=condition=Ready node --all --timeout=120s
  k get storageclass standard >/dev/null || die "StorageClass 'standard' missing"
  k get nodes
}

step_metrics() {
  log "metrics-server"
  k apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
  if ! k -n kube-system get deploy metrics-server \
       -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q kubelet-insecure-tls; then
    k -n kube-system patch deployment metrics-server --type=json \
      -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
  fi
  k -n kube-system rollout status deployment/metrics-server --timeout=180s
}

step_objectstore() {
  log "Object store (SeaweedFS S3 in namespace $S3_NS)"
  cat > "$WORKDIR/objectstore.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata: {name: $S3_NS}
---
apiVersion: v1
kind: Secret
metadata: {name: s3-config, namespace: $S3_NS}
stringData:
  s3.json: |
    {"identities":[{"name":"kubecost",
      "credentials":[{"accessKey":"$S3_ACCESS_KEY","secretKey":"$S3_SECRET_KEY"}],
      "actions":["Admin","Read","Write","List","Tagging"]}]}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: s3-data, namespace: $S3_NS}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard
  resources: {requests: {storage: 10Gi}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: s3, namespace: $S3_NS}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app: s3}}
  template:
    metadata: {labels: {app: s3}}
    spec:
      containers:
        - name: seaweedfs
          image: chrislusf/seaweedfs:latest
          args: ["server", "-dir=/data", "-master.volumeSizeLimitMB=1024",
                 "-s3", "-s3.port=8333", "-s3.config=/etc/seaweedfs/s3.json"]
          ports: [{containerPort: 8333}]
          readinessProbe: {tcpSocket: {port: 8333}, initialDelaySeconds: 5}
          resources: {requests: {cpu: 50m, memory: 128Mi}}
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: config, mountPath: /etc/seaweedfs, readOnly: true}
      volumes:
        - {name: data, persistentVolumeClaim: {claimName: s3-data}}
        - {name: config, secret: {secretName: s3-config}}
---
apiVersion: v1
kind: Service
metadata: {name: s3, namespace: $S3_NS}
spec:
  selector: {app: s3}
  ports: [{port: 9000, targetPort: 8333}]
EOF
  k apply -f "$WORKDIR/objectstore.yaml"
  k -n "$S3_NS" rollout status deployment/s3 --timeout=300s

  log "Bucket $BUCKET"
  k -n "$S3_NS" delete pod s3-mkbucket --ignore-not-found >/dev/null
  local i
  for i in 1 2 3 4 5; do
    if k -n "$S3_NS" run s3-mkbucket --rm -i --restart=Never --image=amazon/aws-cli \
         --env "AWS_ACCESS_KEY_ID=$S3_ACCESS_KEY" --env "AWS_SECRET_ACCESS_KEY=$S3_SECRET_KEY" \
         --env AWS_DEFAULT_REGION=us-east-1 --command -- sh -c \
         "E=http://s3:9000; aws --endpoint-url \$E s3api head-bucket --bucket $BUCKET 2>/dev/null || aws --endpoint-url \$E s3 mb s3://$BUCKET; aws --endpoint-url \$E s3 ls"; then
      return 0
    fi
    echo "S3 not ready yet, retrying ($i/5)..."; sleep 10
    k -n "$S3_NS" delete pod s3-mkbucket --ignore-not-found >/dev/null
  done
  die "Could not create bucket $BUCKET"
}

step_secrets() {
  log "Kubecost secrets"
  k create namespace kubecost --dry-run=client -o yaml | k apply -f -

  cat > "$TMP/federated-store.yaml" <<EOF
type: S3
config:
  bucket: "$BUCKET"
  endpoint: "$S3_ENDPOINT"
  region: "us-east-1"
  insecure: true
  access_key: "$S3_ACCESS_KEY"
  secret_key: "$S3_SECRET_KEY"
EOF
  k -n kubecost create secret generic federated-store \
    --from-file=federated-store.yaml="$TMP/federated-store.yaml" \
    --dry-run=client -o yaml | k apply -f -

  local keyfile key
  if [[ "${KUBECOST_FREE:-0}" == 1 ]] || { [[ -z "${KUBECOST_LICENSE_KEY:-}" ]] && [[ ! -f "$LICENSE_FILE" ]]; }; then
    echo "No license given: Kubecost will run on the free tier (removing any old product-key secret)."
    k -n kubecost delete secret product-key --ignore-not-found
    step_signing
    k -n kubecost get secrets federated-store "$SIGN_PRIV_SECRET" "$SIGN_PUB_SECRET"
    return 0
  fi
  if [[ -n "${KUBECOST_LICENSE_KEY:-}" ]]; then
    key="$KUBECOST_LICENSE_KEY"
  elif [[ -f "$LICENSE_FILE" ]]; then
    key="$(sed -nE 's/.*"key"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' "$LICENSE_FILE" | head -1)"
  else
    die "No license: export KUBECOST_LICENSE_KEY or create $LICENSE_FILE"
  fi
  # strip whitespace and terminal bracketed-paste markers (ESC[200~ ... ESC[201~)
  key="$(printf '%s' "$key" | LC_ALL=C sed -e $'s/\x1b\\[20[01]~//g' -e 's/^\[\{0,1\}200~//' -e 's/\[\{0,1\}201~$//' | tr -d '[:space:]')"
  [[ -n "$key" ]] || die "License key is empty"
  # Kubecost's decoder is strict: restore '=' padding lost when the key was copied
  if [[ "$key" != *.* ]]; then
    while (( ${#key} % 4 )); do key="${key}="; done
  fi
  [[ "$key" =~ ^[A-Za-z0-9+/_.=-]+$ ]] \
    || die "License key has unexpected characters (check for <, >, quotes, ~). First char: '${key:0:1}'"
  keyfile="$TMP/productkey.json"
  printf '{ "key": "%s" }\n' "$key" > "$keyfile"
  k -n kubecost create secret generic product-key \
    --from-file=productkey.json="$keyfile" \
    --dry-run=client -o yaml | k apply -f -
  k -n kubecost get secrets federated-store product-key
}

step_signing() {
  log "Actions signing keys (free tier)"
  mkdir -p "$SIGN_DIR"
  if [[ ! -f "$SIGN_DIR/priv.pem" ]]; then
    ( umask 077; openssl genrsa -out "$SIGN_DIR/priv.pem" 2048 2>/dev/null )
    openssl rsa -in "$SIGN_DIR/priv.pem" -pubout -out "$SIGN_DIR/pub.pem" 2>/dev/null
    echo "Generated RSA key pair in $SIGN_DIR"
  fi
  [[ -f "$SIGN_DIR/pub.pem" ]] || openssl rsa -in "$SIGN_DIR/priv.pem" -pubout -out "$SIGN_DIR/pub.pem" 2>/dev/null
  k create namespace kubecost --dry-run=client -o yaml | k apply -f -
  # Private key for the aggregator (chart expects data key priv.pem)
  k -n kubecost create secret generic "$SIGN_PRIV_SECRET" \
    --from-file=priv.pem="$SIGN_DIR/priv.pem" --dry-run=client -o yaml | k apply -f -
  # Public key for the cluster controller. The chart comment names the file priv.pem,
  # so the public key is stored under that name and the usual ones too.
  k -n kubecost create secret generic "$SIGN_PUB_SECRET" \
    --from-file=priv.pem="$SIGN_DIR/pub.pem" --from-file=pub.pem="$SIGN_DIR/pub.pem" \
    --from-file=public.pem="$SIGN_DIR/pub.pem" --dry-run=client -o yaml | k apply -f -
}

step_kubecost() {
  log "Kubecost (Actions enabled)"
  local licensed=false sign=false
  k -n kubecost get secret product-key >/dev/null 2>&1 && licensed=true
  if [[ "$licensed" == false ]]; then
    k -n kubecost get secret "$SIGN_PRIV_SECRET" >/dev/null 2>&1 || step_signing
    sign=true
    echo "Free tier: Actions signed with the RSA keys in $SIGN_DIR"
  fi
  cat > "$WORKDIR/values.yaml" <<EOF
finopsAgent:
  enabled: true
global:
  acknowledged: true
  clusterId: $CLUSTER
  federatedStorage:
    enabled: true
    existingSecret: federated-store
  storageClassName: standard
clusterController:
  signing:
    rsa:
      enabled: $sign
      publicKeySecret: "$( [[ $sign == true ]] && echo "$SIGN_PUB_SECRET" )"
kubecostProductConfigs:
  productKey:
    enabled: $licensed
    secret: product-key
  actions:
    enabled: true
    signing:
      rsa:
        enabled: $sign
        privateKeySecret: "$( [[ $sign == true ]] && echo "$SIGN_PRIV_SECRET" )"
    config:
      global:
        containerRequestRightSizing: {enabled: true}
        namespaceTurndown: {enabled: true}
        resourceQuotaRightSizing: {enabled: true}
telemetry:
  enabled: false
EOF
  local devel=()
  [[ "${KUBECOST_DEVEL:-0}" == 1 ]] && devel=(--devel)   # pre-release charts only if asked
  helm --kube-context "$CTX" upgrade -i kubecost kubecost \
    --repo https://kubecost.github.io/kubecost \
    -n kubecost --create-namespace ${devel[@]+"${devel[@]}"} -f "$WORKDIR/values.yaml"
  helm --kube-context "$CTX" list -n kubecost

  echo "Waiting for workloads (up to 15 min)..."
  local r
  for r in $(k -n kubecost get deploy,statefulset -o name); do
    k -n kubecost rollout status "$r" --timeout=15m || warn "$r not ready"
  done
  k -n kubecost get pods
  echo
  echo "If anything is unhealthy:"
  echo "  kubectl -n kubecost get events --sort-by=.lastTimestamp | tail -20"
  echo "  kubectl -n kubecost logs <pod> | grep -iE 'bucket|s3|dns|federat|objstore'"
}

step_workloads() {
  log "kcresize sample workloads (lab/workloads.yaml)"
  k apply -f "$LAB_DIR/workloads.yaml"
  local d
  for d in tbm-payments/fraud-scorer tbm-payments/checkout-api shop-prod/api shop-dev/web; do
    k -n "${d%%/*}" rollout status "deploy/${d#*/}" --timeout=180s || warn "$d not ready"
  done
  [[ "$LEGACY_WORKLOADS" == 1 ]] || return 0
  log "Legacy sample workloads (resource-hog, oversized-idle)"
  # python:alpine is multi-arch (Apple Silicon safe); polinux/stress is amd64-only.
  cat > "$WORKDIR/turndown-test.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata: {name: kc-turndown-test, labels: {purpose: kubecost-action-qa}}
---
apiVersion: v1
kind: Namespace
metadata: {name: kc-rightsizing-test, labels: {purpose: kubecost-action-qa}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: resource-hog, namespace: kc-turndown-test}
spec:
  replicas: $HOG_REPLICAS
  selector: {matchLabels: {app: resource-hog}}
  template:
    metadata: {labels: {app: resource-hog}}
    spec:
      containers:
        - name: stress
          image: python:3.12-alpine
          command: ["python3", "-c"]
          args: ["b = b'x' * (384 << 20)\nwhile True: pass"]
          resources:
            requests: {cpu: 500m, memory: 512Mi}
            limits:   {cpu: "$HOG_CPU_LIMIT", memory: 768Mi}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: oversized-idle, namespace: kc-rightsizing-test}
spec:
  replicas: 1
  selector: {matchLabels: {app: oversized-idle}}
  template:
    metadata: {labels: {app: oversized-idle}}
    spec:
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.9
          resources:
            requests: {cpu: 500m, memory: 512Mi}
EOF
  if [[ "$WITH_QUOTA" == 1 ]]; then
    cat >> "$WORKDIR/turndown-test.yaml" <<'EOF'
---
apiVersion: v1
kind: ResourceQuota
metadata: {name: oversized-quota, namespace: kc-rightsizing-test}
spec:
  hard: {requests.cpu: "4", requests.memory: 8Gi}
EOF
  fi
  k apply -f "$WORKDIR/turndown-test.yaml"
  k -n kc-turndown-test    rollout status deploy/resource-hog   --timeout=180s \
    || warn "resource-hog not ready - if Pending, try HOG_REPLICAS=1"
  k -n kc-rightsizing-test rollout status deploy/oversized-idle --timeout=180s
}

step_status() {
  log "Status"
  k get nodes
  k top nodes 2>/dev/null || warn "metrics not ready yet"
  k get pods -n kubecost
  k get pods -A -l 'app in (fraud-scorer,checkout-api,api,web,resource-hog,oversized-idle)' 2>/dev/null || true
  k top pods -n kc-turndown-test    2>/dev/null || true
  k top pods -n kc-rightsizing-test 2>/dev/null || true
  k get resourcequota -n kc-rightsizing-test 2>/dev/null || true
  if k -n kubecost get deploy cloudflared >/dev/null 2>&1; then
    echo "Tunnel: running -> $(tunnel_url)"
  else
    echo "Tunnel: not running"
  fi
}

step_ui() {
  local svc
  svc="$(k -n kubecost get svc -o name | grep -m1 frontend || true)"
  [[ -n "$svc" ]] || die "No frontend service found: kubectl -n kubecost get svc"
  log "Kubecost UI on http://localhost:$UI_PORT ($svc) - Ctrl+C to stop"
  echo "Wait 5-10 min after install before creating Actions (Savings > Actions)."
  k -n kubecost port-forward "$svc" "$UI_PORT:9090"
}

step_expose() {
  log "Always-on UI (NodePort $UI_NODEPORT -> localhost:$UI_PORT)"
  local svc sel tport
  svc="$(k -n kubecost get svc -o name | grep -m1 frontend || true)"
  [[ -n "$svc" ]] || die "No frontend service found: kubectl -n kubecost get svc"
  sel="$(k -n kubecost get "$svc" -o jsonpath='{.spec.selector}')"
  tport="$(k -n kubecost get "$svc" -o jsonpath='{.spec.ports[0].targetPort}')"
  # Separate Service (not Helm-managed) so helm upgrades never revert it.
  k apply -f - <<EOF
apiVersion: v1
kind: Service
metadata: {name: kubecost-ui, namespace: kubecost}
spec:
  type: NodePort
  selector: $sel
  ports: [{port: 9090, targetPort: $tport, nodePort: $UI_NODEPORT}]
EOF
  if docker port "${CLUSTER}-control-plane" 2>/dev/null | grep -q "^$UI_NODEPORT/"; then
    echo "UI: http://localhost:$UI_PORT"
  else
    warn "This cluster was created without the port mapping."
    warn "Use './lab/kc-lab.sh ui' for now, or recreate: ./lab/kc-lab.sh destroy up"
  fi
}

step_clean() {
  log "Removing sample workloads (Kubecost stays)"
  k delete ns -l purpose=kubecost-action-qa --ignore-not-found --wait=true
}

step_reset() { step_clean; step_workloads; }

step_uninstall() {
  log "Removing Kubecost and the object store (cluster stays)"
  helm --kube-context "$CTX" uninstall kubecost -n kubecost 2>/dev/null || true
  k delete ns kubecost "$S3_NS" minio --ignore-not-found
}

step_destroy() {
  read -r -p "Delete cluster $CLUSTER (UI port $UI_PORT) and all its Kubecost data? Other labs are not touched. [y/N] " a
  [[ "$a" == [yY] ]] || { echo "Cancelled."; return; }
  log "Destroying lab"
  kind delete cluster --name "$CLUSTER"
  if [[ "$(uname)" == Darwin && "$STOP_COLIMA_ON_DESTROY" == 1 ]]; then
    if [[ -z "$(kind get clusters 2>/dev/null)" ]]; then colima stop --profile "$COLIMA_PROFILE"
    else echo "Other kind clusters still running ($(kind get clusters | tr '\n' ' ')); Colima left running."; fi
  fi
}

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

step_preflight() {
  log "Preflight for cluster $CLUSTER (UI port $UI_PORT)"
  local others ok=1 vm_cpu="" vm_mem="" vm_disk_free=""
  others="$(kind get clusters 2>/dev/null | grep -vx "$CLUSTER" | tr '\n' ' ' || true)"
  echo "Other kind clusters (left untouched): ${others:-none}"
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    echo "Cluster $CLUSTER already exists: 'up' will reuse it."
  elif port_busy "$UI_PORT"; then
    warn "Port $UI_PORT is in use. Pick another UI_PORT in lab/lab.env."; ok=0
  else
    echo "Port $UI_PORT is free."
  fi
  if [[ "$(uname)" == Darwin ]] && colima status --profile "$COLIMA_PROFILE" >/dev/null 2>&1; then
    vm_cpu="$(colima ssh --profile "$COLIMA_PROFILE" -- nproc 2>/dev/null || true)"
    vm_mem="$(colima ssh --profile "$COLIMA_PROFILE" -- sh -c "awk '/MemTotal/{printf \"%d\", \$2/1048576}' /proc/meminfo" 2>/dev/null || true)"
    vm_disk_free="$(colima ssh --profile "$COLIMA_PROFILE" -- sh -c "df -BG --output=avail / | tail -1 | tr -dc 0-9" 2>/dev/null || true)"
    echo "Colima VM ($COLIMA_PROFILE): ${vm_cpu:-?} vCPU, ${vm_mem:-?} GiB RAM, ${vm_disk_free:-?} GiB disk free"
    echo "Load right now: $(colima ssh --profile "$COLIMA_PROFILE" -- cat /proc/loadavg 2>/dev/null | cut -d' ' -f1-3) (1/5/15 min)"
    [[ -n "$vm_disk_free" && "$vm_disk_free" -lt 15 ]] && { warn "Less than 15 GiB free in the VM: a second Kubecost needs ~8-12 GiB (images + data)."; ok=0; }
    [[ -n "$vm_mem" && "$vm_mem" -lt 12 ]] && { warn "Less than 12 GiB VM memory: two Kubecost labs will be tight."; ok=0; }
    [[ -n "$vm_cpu" && "$vm_cpu" -lt 6 && -n "$others" ]] && warn "Under 6 vCPU with another lab running: expect slow Kubecost; consider pausing the other lab (docker stop <name>-control-plane)."
  fi
  echo "Host disk free: $(df -h "$LAB_DIR" | awk 'NR==2{print $4}')"
  echo "One extra lab typically adds ~2-3.5 CPU cores of real usage (Kubecost ~1.8, kube-system, sample apps), ~3-4 GiB RAM used (~11 GiB requested), ~8-12 GiB disk."
  [[ "$ok" == 1 ]] && echo "preflight: OK" || die "preflight: fix the warnings above (or rerun with SKIP_PREFLIGHT=1)"
}

frontend_svc() {
  local svc
  svc="$(k -n kubecost get svc -o name | grep -m1 frontend || true)"
  [[ -n "$svc" ]] || die "No Kubecost frontend service found: kubectl -n kubecost get svc"
  echo "${svc#service/}"
}

tunnel_url() {
  if [[ -n "$SHARE_HOSTNAME" ]]; then
    echo "https://$SHARE_HOSTNAME"
  else
    k -n kubecost logs deploy/cloudflared 2>/dev/null \
      | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1
  fi
}

step_auth() {
  log "Basic-auth proxy (kubecost-auth)"
  if [[ ! -f "$AUTH_FILE" ]]; then
    umask 077
    printf 'BASIC_USER=%s\nBASIC_PASS=%s\n' "$BASIC_USER" "$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)" > "$AUTH_FILE"
    echo "Generated credentials in $AUTH_FILE"
  fi
  # shellcheck disable=SC1090
  source "$AUTH_FILE"

  local svc port
  svc="$(frontend_svc)"
  port="$(k -n kubecost get svc "$svc" -o jsonpath='{.spec.ports[0].port}')"

  printf '%s:%s\n' "$BASIC_USER" "$(openssl passwd -apr1 "$BASIC_PASS")" > "$TMP/htpasswd"
  k -n kubecost create secret generic kubecost-auth-htpasswd \
    --from-file=htpasswd="$TMP/htpasswd" --dry-run=client -o yaml | k apply -f -

  k apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata: {name: kubecost-auth-nginx, namespace: kubecost}
data:
  default.conf: |
    server {
      listen 8080;
      location = /healthz { access_log off; return 200 "ok"; }
      location / {
        auth_basic "kubecost";
        auth_basic_user_file /etc/nginx/auth/htpasswd;
        proxy_pass http://$svc.kubecost.svc.cluster.local:$port;
        proxy_set_header Host \$host;
        proxy_set_header Authorization "";
        proxy_read_timeout 300s;
      }
    }
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: kubecost-auth, namespace: kubecost}
spec:
  replicas: 1
  selector: {matchLabels: {app: kubecost-auth}}
  template:
    metadata:
      labels: {app: kubecost-auth}
      annotations: {kc-lab/htpasswd: "$(cksum "$TMP/htpasswd" | cut -d" " -f1)"}
    spec:
      containers:
        - name: nginx
          image: nginx:1.27-alpine
          ports: [{containerPort: 8080}]
          readinessProbe: {httpGet: {path: /healthz, port: 8080}}
          resources: {requests: {cpu: 10m, memory: 16Mi}}
          volumeMounts:
            - {name: conf, mountPath: /etc/nginx/conf.d}
            - {name: auth, mountPath: /etc/nginx/auth, readOnly: true}
      volumes:
        - {name: conf, configMap: {name: kubecost-auth-nginx}}
        - {name: auth, secret: {secretName: kubecost-auth-htpasswd}}
---
apiVersion: v1
kind: Service
metadata: {name: kubecost-auth, namespace: kubecost}
spec:
  selector: {app: kubecost-auth}
  ports: [{port: 8080, targetPort: 8080}]
EOF
  k -n kubecost rollout status deploy/kubecost-auth --timeout=120s
}

step_share() {
  step_auth
  # shellcheck disable=SC1090
  source "$AUTH_FILE"
  local upstream="http://kubecost-auth.kubecost.svc.cluster.local:8080"
  local args

  if [[ -n "$SHARE_HOSTNAME" ]]; then
    log "Named Cloudflare Tunnel $TUNNEL_NAME -> https://$SHARE_HOSTNAME"
    command -v cloudflared >/dev/null || brew install cloudflared
    [[ -f "$CF_DIR/cert.pem" ]] || { echo "Opening browser: pick the Cloudflare zone for $SHARE_HOSTNAME"; cloudflared tunnel login; }
    local id
    id="$(cloudflared tunnel list -n "$TUNNEL_NAME" 2>/dev/null | awk -v n="$TUNNEL_NAME" '$2==n{print $1}')"
    if [[ -z "$id" ]]; then
      cloudflared tunnel create "$TUNNEL_NAME"
      id="$(cloudflared tunnel list -n "$TUNNEL_NAME" | awk -v n="$TUNNEL_NAME" '$2==n{print $1}')"
    fi
    [[ -n "$id" && -f "$CF_DIR/$id.json" ]] || die "Tunnel credentials not found for $TUNNEL_NAME"
    cloudflared tunnel route dns --overwrite-dns "$TUNNEL_NAME" "$SHARE_HOSTNAME" || true
    k -n kubecost create secret generic cloudflared-creds \
      --from-file=credentials.json="$CF_DIR/$id.json" --dry-run=client -o yaml | k apply -f -
    cat > "$TMP/config.yml" <<EOF
tunnel: $id
credentials-file: /etc/cloudflared/creds/credentials.json
ingress:
  - hostname: $SHARE_HOSTNAME
    service: $upstream
  - service: http_status:404
EOF
    k -n kubecost create configmap cloudflared-config \
      --from-file=config.yml="$TMP/config.yml" --dry-run=client -o yaml | k apply -f -
    args='["tunnel","--no-autoupdate","--config","/etc/cloudflared/config/config.yml","run"]'
  else
    log "Quick Cloudflare Tunnel (random URL, changes if the pod restarts)"
    args="[\"tunnel\",\"--no-autoupdate\",\"--url\",\"$upstream\"]"
  fi

  k apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: {name: cloudflared, namespace: kubecost}
spec:
  replicas: 1
  selector: {matchLabels: {app: cloudflared}}
  template:
    metadata: {labels: {app: cloudflared}}
    spec:
      containers:
        - name: cloudflared
          image: cloudflare/cloudflared:latest
          args: $args
          resources: {requests: {cpu: 10m, memory: 32Mi}}
          volumeMounts:
            - {name: config, mountPath: /etc/cloudflared/config}
            - {name: creds, mountPath: /etc/cloudflared/creds, readOnly: true}
      volumes:
        - name: config
          configMap: {name: cloudflared-config, optional: true}
        - name: creds
          secret: {secretName: cloudflared-creds, optional: true}
EOF
  k -n kubecost rollout status deploy/cloudflared --timeout=120s

  echo "Waiting for the public URL..."
  local url="" anon="" authd="" i
  for i in $(seq 1 30); do
    sleep 3
    url="$(tunnel_url)"
    [[ -n "$url" ]] || continue
    anon="$(curl -s -o /dev/null -w '%{http_code}' "$url/model/allocation?window=1d" || true)"
    [[ "$anon" =~ ^(000|502|503|530)$ ]] || break
  done
  [[ -n "$url" ]] || { step_unshare; die "No tunnel URL - kubectl -n kubecost logs deploy/cloudflared"; }

  # Safety gate: anonymous calls must be rejected.
  if [[ "$anon" != 401 ]]; then
    step_unshare
    die "Expected HTTP 401 without credentials, got $anon. Tunnel stopped."
  fi
  authd="$(curl -s -o /dev/null -w '%{http_code}' -u "$BASIC_USER:$BASIC_PASS" \
    "$url/model/allocation?window=1d&aggregate=namespace" || true)"
  echo "Without credentials: HTTP $anon (blocked). With credentials: HTTP $authd."
  [[ "$authd" == 200 ]] || warn "Authenticated call did not return 200 - check Kubecost is up and has data"

  cat <<EOF

Apptio Datalink REST 2.0 connector:
  Base URL:  $url
  Endpoint:  /model/allocation?window=1d&aggregate=namespace&accumulate=true
  Method:    GET
  Auth:      Basic   user: $BASIC_USER   password: see $AUTH_FILE
Stop: ./lab/kc-lab.sh unshare    Status: ./lab/kc-lab.sh status
EOF
  [[ -z "$SHARE_HOSTNAME" ]] && warn "Quick-tunnel URL changes whenever the cloudflared pod restarts. Use SHARE_HOSTNAME for a fixed URL."
  return 0
}

step_unshare() {
  log "Stopping tunnel"
  k -n kubecost delete deploy cloudflared --ignore-not-found
}

[[ $# -gt 0 ]] || { awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "$0"; exit 1; }
for s in "$@"; do
  case "$s" in
    up) [[ "${SKIP_PREFLIGHT:-0}" == 1 ]] || { step_tools; step_runtime; step_preflight; }
        for x in tools runtime cluster metrics objectstore secrets kubecost expose workloads; do "step_$x"; done
        log "Done. UI: http://localhost:$UI_PORT   Status: ./lab/kc-lab.sh status" ;;
    minio) step_objectstore ;;
    tools|runtime|cluster|metrics|objectstore|secrets|kubecost|expose|workloads|status|ui|reset|clean|uninstall|destroy|auth|share|unshare|signing|preflight) "step_$s" ;;
    *) die "Unknown step: $s" ;;
  esac
done
