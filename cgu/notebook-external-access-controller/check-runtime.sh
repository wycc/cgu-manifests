#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-cgu}"
SELECTOR="${SELECTOR:-control-plane=notebook-external-access-controller}"
CM_PREFIX="${CM_PREFIX:-notebook-external-access-}"
FAIL=0
FAILED_ITEMS=()

DEPLOY_NAME="notebook-external-access-controller-manager"
SERVICE_NAME="notebook-external-access-notebook-webhook-service"
CLUSTERROLE_NAME="notebook-external-access-notebook-external-access-controller-manager-role"
CLUSTERROLEBINDING_NAME="notebook-external-access-notebook-external-access-controller-manager-rolebinding"
CERT_NAME="notebook-external-access-notebook-serving-cert"
WEBHOOK_NAME="notebook-external-access-notebook-validating-webhook-configuration"

mark_fail() {
  local item="$1"
  FAILED_ITEMS+=("${item}")
  FAIL=1
}

check_exists() {
  local kind="$1"
  local name="$2"
  local ns="${3:-}"

  if [[ -n "${ns}" ]]; then
    if kubectl -n "${ns}" get "${kind}" "${name}" >/dev/null 2>&1; then
      echo "[OK] ${kind}/${name} in ${ns}"
    else
      echo "[MISS] ${kind}/${name} in ${ns}"
      mark_fail "${kind}/${name} in ${ns}"
    fi
  else
    if kubectl get "${kind}" "${name}" >/dev/null 2>&1; then
      echo "[OK] ${kind}/${name}"
    else
      echo "[MISS] ${kind}/${name}"
      mark_fail "${kind}/${name}"
    fi
  fi
}

debug_pods_by_selector() {
  echo "[DEBUG] pod status for selector=${SELECTOR}"
  kubectl -n "${NAMESPACE}" get pod -l "${SELECTOR}" \
    -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount,REASON:.status.containerStatuses[*].state.waiting.reason \
    --no-headers 2>/dev/null || true

  local pod
  pod="$(kubectl -n "${NAMESPACE}" get pod -l "${SELECTOR}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -n "${pod}" ]]; then
    echo "[DEBUG] describe pod/${pod} (first 120 lines)"
    kubectl -n "${NAMESPACE}" describe pod "${pod}" | sed -n '1,120p' || true
    echo "[DEBUG] logs pod/${pod} (tail 80)"
    kubectl -n "${NAMESPACE}" logs "${pod}" --tail=80 || true
    echo "[DEBUG] previous logs pod/${pod} (tail 80, if restarted)"
    kubectl -n "${NAMESPACE}" logs "${pod}" --previous --tail=80 || true
  fi
}

check_deploy_ready() {
  if kubectl -n "${NAMESPACE}" get deploy "${DEPLOY_NAME}" >/dev/null 2>&1; then
    local desired ready
    desired="$(kubectl -n "${NAMESPACE}" get deploy "${DEPLOY_NAME}" -o jsonpath='{.status.replicas}')"
    ready="$(kubectl -n "${NAMESPACE}" get deploy "${DEPLOY_NAME}" -o jsonpath='{.status.readyReplicas}')"
    desired="${desired:-0}"
    ready="${ready:-0}"
    if [[ "${ready}" -ge 1 ]]; then
      echo "[OK] deploy/${DEPLOY_NAME} readyReplicas=${ready} replicas=${desired}"
    else
      echo "[WARN] deploy/${DEPLOY_NAME} readyReplicas=${ready} replicas=${desired}"
      mark_fail "deploy/${DEPLOY_NAME} not ready (readyReplicas=${ready}, replicas=${desired})"
      debug_pods_by_selector
    fi
  else
    echo "[MISS] deploy/${DEPLOY_NAME} in ${NAMESPACE}"
    mark_fail "deploy/${DEPLOY_NAME} in ${NAMESPACE}"
  fi
}

check_pods() {
  local count
  count="$(kubectl -n "${NAMESPACE}" get pod -l "${SELECTOR}" --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "${count}" -ge 1 ]]; then
    echo "[OK] pods with selector ${SELECTOR}: ${count}"
  else
    echo "[MISS] pods with selector ${SELECTOR}: 0"
    mark_fail "pods with selector ${SELECTOR} in ${NAMESPACE}"
  fi
}

check_prefixed_configmaps() {
  local count
  count="$(kubectl -n "${NAMESPACE}" get cm -o name 2>/dev/null | sed 's#^configmap/##' | grep -E "^${CM_PREFIX}" | wc -l | tr -d ' ' || true)"
  echo "[INFO] configmaps with prefix ${CM_PREFIX} in ${NAMESPACE}: ${count}"
}

echo "[INFO] Checking notebook-external-access-controller resources"
echo "[INFO] Namespace: ${NAMESPACE}"
echo "[INFO] Selector: ${SELECTOR}"

echo "[CHECK] Deployment"
check_deploy_ready

echo "[CHECK] Pods"
check_pods

echo "[CHECK] Service"
check_exists service "${SERVICE_NAME}" "${NAMESPACE}"

echo "[CHECK] RBAC"
check_exists clusterrole "${CLUSTERROLE_NAME}"
check_exists clusterrolebinding "${CLUSTERROLEBINDING_NAME}"

echo "[CHECK] Webhook/Cert"
check_exists validatingwebhookconfiguration "${WEBHOOK_NAME}"
check_exists certificate "${CERT_NAME}" "${NAMESPACE}"

echo "[CHECK] ConfigMaps"
check_prefixed_configmaps

if [[ "${FAIL}" -eq 0 ]]; then
  echo "[DONE] All required resources are present"
  exit 0
else
  echo "[SUMMARY] Failed checks:"
  for item in "${FAILED_ITEMS[@]}"; do
    echo "- ${item}"
  done
  echo "[DONE] Some required resources are missing or not ready"
  exit 1
fi
