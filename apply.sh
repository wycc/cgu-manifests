#!/bin/bash
MASTER_IP=
QNAP_IP=
QNAP_USERNAME=
QNAP_PASSWORD=
QNAP_SHARED_NAMESPACE=
QNAP_NFS_DEPLOYMENT=
APPLY_SKIP_NAMESPACES=
source env.ini
# Optional: comma-separated namespaces this cluster does not run (see preserve_live_state.py).
export APPLY_SKIP_NAMESPACES
if [ -z "$MASTER_IP" ]; then
	echo "Please setup the environment variable MASTER_IP to be the IP address of your master in env.ini"
	exit;
fi
if [ -z "$QNAP_IP" ]; then
	echo "Please setup the environment variable QNAP_IP,QNAP_USERNAME,QNAP_PASSWORD in env.ini"
	exit;
fi

if [ -z "$QNAP_PASSWORD" ]; then
	echo "Please setup the environment variable QNAP_PASSWORD in env.ini"
	exit;
fi

if [ -z "$QNAP_USERNAME" ]; then
	echo "Please setup the environment variable QNAP_USERNAME in env.ini"
	exit;
fi

if [ -z "$QNAP_MOUNT" ]; then
  echo "Please setup the environment variable QNAP_MOUNT in env.ini"
  exit;
fi

QNAP_MOUNT="${QNAP_MOUNT%/}"
if [ -z "$QNAP_MOUNT" ]; then
  echo "QNAP_MOUNT in env.ini is invalid"
  exit;
fi

if [ -z "$QNAP_SHARED_NAMESPACE" ]; then
  echo "Please setup the environment variable QNAP_SHARED_NAMESPACE in env.ini"
  exit;
fi

if [ -z "$QNAP_NFS_DEPLOYMENT" ]; then
  echo "Please setup the environment variable QNAP_NFS_DEPLOYMENT in env.ini"
  exit;
fi

escape_sed_replacement() {
  # Escape replacement text for sed: & and backslash, plus | because the seds below use it as delimiter.
  printf '%s' "$1" | sed -e 's/[&\\|]/\\&/g'
}

GROUPSHARE_CONTROLLER_DEPLOY="cgu/groupshare/deploy/controller-deployment.yaml"
NAMESPACE_SHARE_CONTROLLER_DEPLOY="cgu/namespace_share/deploy/controller-deployment.yaml"
GROUPSHARE_CONTROLLER_CONFIGMAP="cgu/groupshare/deploy/controller-configmap.yaml"
NAMESPACE_SHARE_CONTROLLER_CONFIGMAP="cgu/namespace_share/deploy/controller-configmap.yaml"
LDAP_BACKEND_DEPLOY="common/dex/overlays/ldap-backend/full-stack-deployment.yaml"
QNAP_IP_ESCAPED="$(escape_sed_replacement "${QNAP_IP}")"
QNAP_MOUNT_ESCAPED="$(escape_sed_replacement "${QNAP_MOUNT}")"
QNAP_SHARED_NAMESPACE_ESCAPED="$(escape_sed_replacement "${QNAP_SHARED_NAMESPACE}")"
QNAP_NFS_DEPLOYMENT_ESCAPED="$(escape_sed_replacement "${QNAP_NFS_DEPLOYMENT}")"
NAMESPACE_SHARE_NFS_PATH_ESCAPED="$(escape_sed_replacement "${QNAP_MOUNT}/_namespaces")"

# Keep groupshare controller env values synced with env.ini before applying manifests.
sed -i "/- name: NFS_NAMESPACE/{n;s|value:.*|value: ${QNAP_SHARED_NAMESPACE_ESCAPED}|;}" "${GROUPSHARE_CONTROLLER_DEPLOY}"
sed -i "/- name: NFS_DEPLOYMENT/{n;s|value:.*|value: ${QNAP_NFS_DEPLOYMENT_ESCAPED}|;}" "${GROUPSHARE_CONTROLLER_DEPLOY}"
sed -i "/- name: NFS_NAMESPACE/{n;s|value:.*|value: ${QNAP_SHARED_NAMESPACE_ESCAPED}|;}" "${NAMESPACE_SHARE_CONTROLLER_DEPLOY}"
sed -i "/- name: NFS_DEPLOYMENT/{n;s|value:.*|value: ${QNAP_NFS_DEPLOYMENT_ESCAPED}|;}" "${NAMESPACE_SHARE_CONTROLLER_DEPLOY}"

# Keep groupshare/namespace-share NFS default ConfigMaps synced with env.ini before applying manifests.
sed -i "s|namespace:.*|namespace: ${QNAP_SHARED_NAMESPACE_ESCAPED}|" "${GROUPSHARE_CONTROLLER_CONFIGMAP}"
sed -i "s|NFS_SERVER:.*|NFS_SERVER: \"${QNAP_IP_ESCAPED}\"|" "${GROUPSHARE_CONTROLLER_CONFIGMAP}"
sed -i "s|NFS_PATH:.*|NFS_PATH: \"${QNAP_MOUNT_ESCAPED}\"|" "${GROUPSHARE_CONTROLLER_CONFIGMAP}"

sed -i "s|namespace:.*|namespace: ${QNAP_SHARED_NAMESPACE_ESCAPED}|" "${NAMESPACE_SHARE_CONTROLLER_CONFIGMAP}"
sed -i "s|NFS_SERVER:.*|NFS_SERVER: \"${QNAP_IP_ESCAPED}\"|" "${NAMESPACE_SHARE_CONTROLLER_CONFIGMAP}"
sed -i "s|NFS_PATH:.*|NFS_PATH: \"${NAMESPACE_SHARE_NFS_PATH_ESCAPED}\"|" "${NAMESPACE_SHARE_CONTROLLER_CONFIGMAP}"

# Keep ldap backend groupshare NFS settings synced with env.ini before applying manifests.
sed -i "/- name: groupshare-storage/{n;n;s|server:.*|server: ${QNAP_IP_ESCAPED}|;n;s|path:.*|path: ${QNAP_MOUNT_ESCAPED}|;}" "${LDAP_BACKEND_DEPLOY}"

# preserve_live_state.py (used below) needs PyYAML; check before touching the cluster.
if ! python3 -c 'import yaml' 2>/dev/null; then
  echo "python3 PyYAML is required (Ubuntu: apt install python3-yaml); nothing was changed."
  exit 1
fi

# GroupShare prerequisites kustomize cannot express (idempotent; existing objects are kept).
# Stop before touching the cluster when one fails: a deployment without NFS directories or webhook
# trust leaves Notebooks unable to start.
groupshare_prerequisite_failed() {
  echo "GroupShare prerequisite '$1' failed; stopped before deploying. Fix it and run apply.sh again."
  exit 1
}
QNAP_IP="${QNAP_IP}" QNAP_MOUNT="${QNAP_MOUNT}" bash cgu/groupshare/scripts/bootstrap.sh nfs || groupshare_prerequisite_failed nfs
bash cgu/groupshare/scripts/bootstrap.sh secrets || groupshare_prerequisite_failed secrets
bash cgu/groupshare/scripts/bootstrap.sh vwc || groupshare_prerequisite_failed vwc
GROUPSHARE_RC=0

# The manifest's ingress Service is ClusterIP (externalIPs are patched in below). Replace an existing
# Service only when its type differs; deleting it on every run cut all users off for the whole apply.
INGRESS_TYPE=$(kubectl -n istio-system get svc istio-ingressgateway -o jsonpath='{.spec.type}' 2>/dev/null)
if [ -n "$INGRESS_TYPE" ] && [ "$INGRESS_TYPE" != "ClusterIP" ]; then
  kubectl delete svc -n istio-system istio-ingressgateway
fi
# preserve_live_state.py keeps state the running cluster owns (existing PVCs, live ConfigMaps,
# injected caBundles, aggregated rules); on a new cluster everything is applied unchanged.
# Custom resources need a few rounds until their CRDs are ready, but never retry forever:
# the ingress, NVIDIA and QNAP steps below must still run.
APPLY_RC=1
for attempt in $(seq 1 15); do
  if kustomize build ./example | python3 preserve_live_state.py | kubectl apply -f -; then
    APPLY_RC=0
    break
  fi
  echo "Retrying to apply resources ($attempt/15)"
  sleep 20
done
kubectl patch svc -n istio-system istio-ingressgateway -p "{\"spec\":{\"externalIPs\":[\"${MASTER_IP}\"]}}"
bash cgu/groupshare/scripts/bootstrap.sh webhook || GROUPSHARE_RC=1
# kubectl patch svc -n istio-system istio-ingressgateway -p '{"spec":{"type":"ClusterIP"}}'
kubectl taint nodes $(hostname -s) node-role.kubernetes.io/control-plane:NoSchedule
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
helm repo update
VER=0.15.1
helm upgrade -i nvdp nvdp/nvidia-device-plugin \
  --version $VER \
  --namespace nvidia-device-plugin \
  --create-namespace \
  --set compatWithCPUManager=true \
  --set gfd.enabled=true\
  --set-file config.map.default=mps.config \
  --set-file config.map.nomps=nomps.config

QNAP_IP_64=`echo -n ${QNAP_IP} | base64`
QNAP_USERNAME_64=`echo -n ${QNAP_USERNAME} | base64`
QNAP_PASSWORD_64=`echo -n ${QNAP_PASSWORD} | base64`

kubectl patch cm qnap-config -n kubeflow -p "{\"data\":{\"ip\":\"${QNAP_IP_64}\",\"username\":\"${QNAP_USERNAME_64}\",\"password\":\"${QNAP_PASSWORD_64}\"}}"
python3 addqnap.py

# Post-deploy GroupShare / NamespaceShare verification (read-only).
bash cgu/groupshare/scripts/check.sh env.ini
if [ "$APPLY_RC" -ne 0 ]; then
  echo "Some resources could not be applied after 15 attempts; see the kubectl errors above."
  exit 1
fi
if [ "$GROUPSHARE_RC" -ne 0 ]; then
  echo "GroupShare webhook is not ready (see bootstrap output above); Notebook create/update is blocked until it is."
  exit 1
fi
