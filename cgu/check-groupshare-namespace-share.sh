#!/usr/bin/env bash
set -euo pipefail

KUBEFLOW_NS="${KUBEFLOW_NS:-kubeflow}"
KF_STORAGE_NS="${KF_STORAGE_NS:-kf-storage}"
EXAMPLE_USER_NS="${EXAMPLE_USER_NS:-kubeflow-user-example-com}"
FAIL=0

check_exists() {
  local kind="$1"
  local name="$2"
  local ns="${3:-}"

  if [[ -n "${ns}" ]]; then
    if kubectl -n "${ns}" get "${kind}" "${name}" >/dev/null 2>&1; then
      echo "[OK] ${kind}/${name} in ${ns}"
    else
      echo "[MISS] ${kind}/${name} in ${ns}"
      FAIL=1
    fi
  else
    if kubectl get "${kind}" "${name}" >/dev/null 2>&1; then
      echo "[OK] ${kind}/${name}"
    else
      echo "[MISS] ${kind}/${name}"
      FAIL=1
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
      FAIL=1
    fi
  else
    echo "[MISS] deploy/${name} in ${KUBEFLOW_NS}"
    FAIL=1
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
    FAIL=1
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
check_pod_by_app groupshare-webhook
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
  echo "[DONE] Some required resources are missing or not ready"
  exit 1
fi
