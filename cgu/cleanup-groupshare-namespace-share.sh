#!/usr/bin/env bash
set -euo pipefail

KUBEFLOW_NS="${KUBEFLOW_NS:-kubeflow}"
KF_STORAGE_NS="${KF_STORAGE_NS:-kf-storage}"
EXAMPLE_USER_NS="${EXAMPLE_USER_NS:-kubeflow-user-example-com}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[INFO] Cleaning GroupShare + NamespaceShare resources"
echo "[INFO] KUBEFLOW_NS=${KUBEFLOW_NS}, KF_STORAGE_NS=${KF_STORAGE_NS}, EXAMPLE_USER_NS=${EXAMPLE_USER_NS}"

echo "[STEP] Delete resources defined by kustomize"
kubectl delete -k "${ROOT_DIR}/groupshare" --ignore-not-found=true || true
kubectl delete -k "${ROOT_DIR}/namespace_share" --ignore-not-found=true || true

echo "[STEP] Delete leftover pods from deployments"
kubectl -n "${KUBEFLOW_NS}" delete pod \
  -l app=groupshare-controller \
  --ignore-not-found=true --grace-period=0 --force || true
kubectl -n "${KUBEFLOW_NS}" delete pod \
  -l app=groupshare-webhook \
  --ignore-not-found=true --grace-period=0 --force || true
kubectl -n "${KUBEFLOW_NS}" delete pod \
  -l app=namespace-share-controller \
  --ignore-not-found=true --grace-period=0 --force || true

echo "[STEP] Delete known webhook/cert leftovers"
kubectl delete validatingwebhookconfiguration groupshare-validating-webhook --ignore-not-found=true || true
kubectl -n "${KUBEFLOW_NS}" delete certificate groupshare-webhook-cert --ignore-not-found=true || true
kubectl -n "${KUBEFLOW_NS}" delete secret groupshare-webhook-certs --ignore-not-found=true || true

echo "[STEP] Delete known configmaps explicitly"
kubectl -n "${KF_STORAGE_NS}" delete configmap groupshare-nfs-defaults namespace-share-nfs-defaults --ignore-not-found=true || true
kubectl -n "${KUBEFLOW_NS}" delete configmap groupshare-controller-code groupshare-webhook-code namespace-share-controller-code --ignore-not-found=true || true

echo "[STEP] Delete example notebook template (if present)"
kubectl -n "${EXAMPLE_USER_NS}" delete notebook default-template-groupshare --ignore-not-found=true || true

echo "[DONE] Cleanup finished"
