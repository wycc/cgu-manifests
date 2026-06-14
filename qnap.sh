#!/bin/bash

source env.ini

if [ -z "${QNAP_IP}" ] || [ -z "${QNAP_MOUNT}" ]; then
	echo "QNAP_IP or QNAP_MOUNT is not defined in env.ini"
	exit 1
fi

QNAP_IP_64=`echo -n ${QNAP_IP} | base64`
QNAP_USERNAME_64=`echo -n ${QNAP_USERNAME} | base64`
QNAP_PASSWORD_64=`echo -n ${QNAP_PASSWORD} | base64`

kubectl patch cm qnap-config -n kubeflow -p "{\"data\":{\"ip\":\"${QNAP_IP_64}\",\"username\":\"${QNAP_USERNAME_64}\",\"password\":\"${QNAP_PASSWORD_64}\"}}"

NFS_PATH="${QNAP_MOUNT}"
echo "Patching ldap/backend-deployment NFS path: ${QNAP_IP}:${NFS_PATH}"
kubectl patch deployment backend-deployment -n ldap --type='strategic' -p "{\"spec\":{\"template\":{\"spec\":{\"volumes\":[{\"name\":\"groupshare-storage\",\"nfs\":{\"server\":\"${QNAP_IP}\",\"path\":\"${NFS_PATH}\"}}]}}}}"

GROUPSHARE_NFS_PATH="${NFS_PATH}"
NAMESPACE_SHARE_NFS_PATH="${NFS_PATH}/_namespaces"

echo "Patching groupshare-nfs-defaults: ${QNAP_IP}:${GROUPSHARE_NFS_PATH}"
kubectl patch cm groupshare-nfs-defaults -n kf-storage --type='merge' -p "{\"data\":{\"NFS_SERVER\":\"${QNAP_IP}\",\"NFS_PATH\":\"${GROUPSHARE_NFS_PATH}\"}}"

echo "Patching namespace-share-nfs-defaults: ${QNAP_IP}:${NAMESPACE_SHARE_NFS_PATH}"
kubectl patch cm namespace-share-nfs-defaults -n kf-storage --type='merge' -p "{\"data\":{\"NFS_SERVER\":\"${QNAP_IP}\",\"NFS_PATH\":\"${NAMESPACE_SHARE_NFS_PATH}\"}}"

kubectl rollout restart deployment/groupshare-controller -n kubeflow
kubectl rollout restart deployment/namespace-share-controller -n kubeflow

python3 addqnap.py
