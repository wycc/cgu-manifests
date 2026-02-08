#!/bin/bash

source env.ini

QNAP_IP_64=`echo -n ${QNAP_IP} | base64`
QNAP_USERNAME_64=`echo -n ${QNAP_USERNAME} | base64`
QNAP_PASSWORD_64=`echo -n ${QNAP_PASSWORD} | base64`

kubectl patch cm qnap-config -n kubeflow -p "{\"data\":{\"ip\":\"${QNAP_IP_64}\",\"username\":\"${QNAP_USERNAME_64}\",\"password\":\"${QNAP_PASSWORD_64}\"}}"
python3 addqnap.py
