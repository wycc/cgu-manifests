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

NFS_PATH='$(QNAP_MOUNT)/shared'
kubectl patch deployment backend-deployment -n ldap --type='strategic' -p "{\"spec\":{\"template\":{\"spec\":{\"volumes\":[{\"name\":\"groupshare-storage\",\"nfs\":{\"server\":\"${QNAP_IP}\",\"path\":\"${NFS_PATH}\"}}]}}}}"

python3 addqnap.py
