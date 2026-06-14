#!/usr/bin/env bash
set -euo pipefail

KUBEFLOW_NS="${KUBEFLOW_NS:-kubeflow}"
KF_STORAGE_NS="${KF_STORAGE_NS:-kf-storage}"
EXAMPLE_USER_NS="${EXAMPLE_USER_NS:-kubeflow-user-example-com}"
FAIL=0
FAILED_ITEMS=()

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

check_deploy_ready() {
  local name="$1"
  if kubectl -n "${KUBEFLOW_NS}" get deploy "${name}" >/dev/null 2>&1; then
    local desired ready
    desired="$(kubectl -n "${KUBEFLOW_NS}" get deploy "${name}" -o jsonpath='{.status.replicas}')"
    ready="$(kubectl -n "${KUBEFLOW_NS}" get deploy "${name}" -o jsonpath='{.status.readyReplicas}')"
    desired="${desired:-0}"
    ready="${ready:-0}"
    if [[ "${ready}" -ge 1 ]]; then
      echo "[OK] deploy/${name} readyReplicas=${ready} replicas=${desired}"
    else
      echo "[WARN] deploy/${name} readyReplicas=${ready} replicas=${desired}"
      mark_fail "deploy/${name} not ready (readyReplicas=${ready}, replicas=${desired})"
      debug_pods_by_app "${name}"
    fi
  else
    echo "[MISS] deploy/${name} in ${KUBEFLOW_NS}"
    mark_fail "deploy/${name} in ${KUBEFLOW_NS}"
  fi
}

debug_pods_by_app() {
  local app="$1"
  local pod

  echo "[DEBUG] pod status for app=${app}"
  kubectl -n "${KUBEFLOW_NS}" get pod -l "app=${app}" \
    -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount,REASON:.status.containerStatuses[*].state.waiting.reason \
    --no-headers 2>/dev/null || true

  pod="$(kubectl -n "${KUBEFLOW_NS}" get pod -l "app=${app}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -n "${pod}" ]]; then
    echo "[DEBUG] describe pod/${pod} (first 120 lines)"
    kubectl -n "${KUBEFLOW_NS}" describe pod "${pod}" | sed -n '1,120p' || true
    echo "[DEBUG] logs pod/${pod} (tail 80)"
    kubectl -n "${KUBEFLOW_NS}" logs "${pod}" --tail=80 || true
    echo "[DEBUG] previous logs pod/${pod} (tail 80, if restarted)"
    kubectl -n "${KUBEFLOW_NS}" logs "${pod}" --previous --tail=80 || true
  fi
}

check_pod_by_app() {
  local app="$1"
  local count
  count="$(kubectl -n "${KUBEFLOW_NS}" get pod -l "app=${app}" --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "${count}" -ge 1 ]]; then
    echo "[OK] pods with app=${app}: ${count}"
  else
    echo "[MISS] pods with app=${app}: 0"
    mark_fail "pods with app=${app} in ${KUBEFLOW_NS}"
  fi
}

echo "[INFO] Checking GroupShare + NamespaceShare resources"
echo "[INFO] KUBEFLOW_NS=${KUBEFLOW_NS}, KF_STORAGE_NS=${KF_STORAGE_NS}, EXAMPLE_USER_NS=${EXAMPLE_USER_NS}"

echo "[CHECK] Deployments"
check_deploy_ready groupshare-controller
check_deploy_ready groupshare-validating-webhook
check_deploy_ready namespace-share-controller

echo "[CHECK] Pods"
check_pod_by_app groupshare-controller
check_pod_by_app groupshare-validating-webhook
check_pod_by_app namespace-share-controller

echo "[CHECK] ConfigMaps"
check_exists configmap groupshare-nfs-defaults "${KF_STORAGE_NS}"
check_exists configmap namespace-share-nfs-defaults "${KF_STORAGE_NS}"
check_exists configmap groupshare-controller-code "${KUBEFLOW_NS}"
check_exists configmap groupshare-webhook-code "${KUBEFLOW_NS}"
check_exists configmap namespace-share-controller-code "${KUBEFLOW_NS}"

echo "[CHECK] RBAC"
check_exists clusterrole groupshare-controller-clusterrole
check_exists clusterrolebinding groupshare-controller-clusterrolebinding
check_exists clusterrole groupshare-webhook-clusterrole
check_exists clusterrolebinding groupshare-webhook-clusterrolebinding
check_exists clusterrole namespace-share-controller-clusterrole
check_exists clusterrolebinding namespace-share-controller-clusterrolebinding

echo "[CHECK] Webhook/Cert"
check_exists validatingwebhookconfiguration groupshare-validating-webhook
check_exists certificate groupshare-webhook-cert "${KUBEFLOW_NS}"

echo "[CHECK] Optional notebook template"
if kubectl -n "${EXAMPLE_USER_NS}" get notebook default-template-groupshare >/dev/null 2>&1; then
  echo "[OK] notebook/default-template-groupshare in ${EXAMPLE_USER_NS}"
else
  echo "[INFO] notebook/default-template-groupshare not found in ${EXAMPLE_USER_NS}"
fi

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
