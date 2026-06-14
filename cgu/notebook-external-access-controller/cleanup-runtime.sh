#!/usr/bin/env bash
set -euo pipefail

# Full cleanup for notebook-external-access-controller.
# Removes deployment/pod/configmap/RBAC/webhook/cert resources and optionally the namespace.

NAMESPACE="${NAMESPACE:-cgu}"
SELECTOR="${SELECTOR:-control-plane=notebook-external-access-controller}"
CM_PREFIX="${CM_PREFIX:-notebook-external-access-}"
DELETE_NAMESPACE="${DELETE_NAMESPACE:-false}"

# Resolve script directory so kubectl delete -k always targets this component.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[INFO] Namespace: ${NAMESPACE}"
echo "[INFO] Selector: ${SELECTOR}"
echo "[INFO] ConfigMap prefix: ${CM_PREFIX}"
echo "[INFO] Delete namespace: ${DELETE_NAMESPACE}"

echo "[STEP] Delete all resources defined by this component kustomization (includes RBAC/webhook/cert/deploy/service)"
kubectl delete -k "${SCRIPT_DIR}" --ignore-not-found=true || true

echo "[STEP] Delete leftover deployments for this controller"
kubectl -n "${NAMESPACE}" delete deploy -l "${SELECTOR}" --ignore-not-found=true || true

echo "[STEP] Delete leftover pods for this controller"
kubectl -n "${NAMESPACE}" delete pod -l "${SELECTOR}" --ignore-not-found=true --grace-period=0 --force || true

echo "[STEP] Delete labeled configmaps for this controller"
kubectl -n "${NAMESPACE}" delete cm -l "${SELECTOR}" --ignore-not-found=true || true

echo "[STEP] Delete prefixed configmaps (if any)"
mapfile -t prefixed_cms < <(
  kubectl -n "${NAMESPACE}" get cm -o name 2>/dev/null \
    | sed 's#^configmap/##' \
    | grep -E "^${CM_PREFIX}" || true
)

if [[ ${#prefixed_cms[@]} -gt 0 ]]; then
  kubectl -n "${NAMESPACE}" delete cm "${prefixed_cms[@]}" --ignore-not-found=true
else
  echo "[INFO] No prefixed configmaps found"
fi

if [[ "${DELETE_NAMESPACE}" == "true" ]]; then
  echo "[STEP] Delete namespace ${NAMESPACE}"
  kubectl delete namespace "${NAMESPACE}" --ignore-not-found=true || true
fi

echo "[DONE] Cleanup finished."
