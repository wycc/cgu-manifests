#!/bin/bash
# Sets what the AI_Centre_Admin billing pages need from kube-prometheus-stack: NodePort 32049
# and 360 days of retention. On .6 both were patched onto the live objects (helm values are
# empty), so this patches rather than running helm upgrade. .6 itself stays on 180 days for
# now, and the check below stops this script there instead of deleting its data.
# Safe to re-run: a value that is already correct is left alone.
set -euo pipefail

NAMESPACE=prometheus
RELEASE=kube-prom-stack-kube-prome-prometheus
NODE_PORT=32049
RETENTION=360d

current_port=$(kubectl -n "$NAMESPACE" get svc "$RELEASE" \
  -o jsonpath='{.spec.ports[?(@.port==9090)].nodePort}')
if [ "$current_port" = "$NODE_PORT" ]; then
  echo "Prometheus NodePort is already $NODE_PORT."
else
  kubectl -n "$NAMESPACE" patch svc "$RELEASE" \
    -p "{\"spec\":{\"type\":\"NodePort\",\"ports\":[{\"port\":9090,\"nodePort\":$NODE_PORT}]}}"
fi

current_retention=$(kubectl -n "$NAMESPACE" get prometheus "$RELEASE" -o jsonpath='{.spec.retention}')
if [ "$current_retention" = "$RETENTION" ]; then
  echo "Prometheus retention is already $RETENTION."
  exit 0
fi

# Retention is a startup flag, so changing it recreates the pod. Without a PVC
# (spec.storage is empty) that deletes everything Prometheus has stored.
storage=$(kubectl -n "$NAMESPACE" get prometheus "$RELEASE" -o jsonpath='{.spec.storage}')
if [ -z "$storage" ] && [ "${CONFIRM_DATA_LOSS:-}" != "yes" ]; then
  echo "Prometheus has no PVC: changing retention from $current_retention to $RETENTION deletes its stored data."
  echo "Run again with CONFIRM_DATA_LOSS=yes to go ahead."
  exit 1
fi
kubectl -n "$NAMESPACE" patch prometheus "$RELEASE" --type=merge \
  -p "{\"spec\":{\"retention\":\"$RETENTION\"}}"
