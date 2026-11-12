# shellcheck shell=bash
# Host port for this lab's Kubecost UI/API. Sourced by lab/kc-lab.sh, lab/setup.sh, lab/e2e-local.sh, kcctl.
#
#   kc_resolve_ui_port   sets and exports UI_PORT (and KUBECOST_URL if it was not set):
#     1. UI_PORT set to a number (env or lab/lab.env)  -> use it as is (an explicit choice)
#     2. this lab's kind cluster already exists         -> the host port it was created with
#     3. a port saved earlier in $WORKDIR/ui-port       -> that port, if still free (stable across down/up)
#     4. otherwise 9090, or the next free port up to 9099 if another instance holds 9090
#   UI_PORT=auto (or empty) means "resolve".  Needs CLUSTER and WORKDIR; UI_NODEPORT defaults to 30090.

KC_DEFAULT_UI_PORT="${KC_DEFAULT_UI_PORT:-9090}"
KC_MAX_UI_PORT="${KC_MAX_UI_PORT:-$((KC_DEFAULT_UI_PORT + 9))}"

kc_port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# Host port bound to the NodePort of an existing kind node (running or stopped), or nothing.
kc_cluster_ui_port() {
  command -v docker >/dev/null 2>&1 || return 0
  docker inspect -f "{{range \$p, \$b := .HostConfig.PortBindings}}{{if eq \$p \"${UI_NODEPORT:-30090}/tcp\"}}{{(index \$b 0).HostPort}}{{end}}{{end}}" \
    "${CLUSTER}-control-plane" 2>/dev/null || true
}

kc_resolve_ui_port() {
  local p="" src="" saved="${WORKDIR:?}/ui-port"
  case "${UI_PORT:-}" in
    ''|auto) ;;
    *[!0-9]*) echo "ERROR: UI_PORT must be a port number or 'auto' (got '$UI_PORT')" >&2; return 1 ;;
    *) p="$UI_PORT"; src="UI_PORT setting" ;;
  esac
  if [ -z "$p" ]; then
    p="$(kc_cluster_ui_port)"
    case "$p" in ''|*[!0-9]*) p="" ;; *) src="existing cluster ${CLUSTER}" ;; esac
  fi
  if [ -z "$p" ] && [ -s "$saved" ]; then           # skipped if something else took it meanwhile
    p="$(tr -cd '0-9' < "$saved")"
    if [ -n "$p" ] && ! kc_port_busy "$p"; then src="saved in $saved"; else p=""; fi
  fi
  if [ -z "$p" ]; then
    p="$KC_DEFAULT_UI_PORT"
    while kc_port_busy "$p"; do
      p=$((p + 1))
      if [ "$p" -gt "$KC_MAX_UI_PORT" ]; then
        echo "ERROR: ports $KC_DEFAULT_UI_PORT-$KC_MAX_UI_PORT are all in use; set UI_PORT in lab/lab.env" >&2
        return 1
      fi
    done
    if [ "$p" = "$KC_DEFAULT_UI_PORT" ]; then src="default"
    else src="$KC_DEFAULT_UI_PORT is in use by another instance"; fi
  fi
  case "$src" in
    "UI_PORT setting") ;;                                    # explicit: nothing to remember
    *) mkdir -p "$WORKDIR" && printf '%s\n' "$p" > "$saved" ;;
  esac
  UI_PORT="$p"
  KC_UI_PORT_SOURCE="$src"
  KUBECOST_URL="${KUBECOST_URL:-http://localhost:$UI_PORT}"
  export UI_PORT KUBECOST_URL KC_UI_PORT_SOURCE
}
