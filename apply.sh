#!/bin/bash
MASTER_IP=
QNAP_IP=
QNAP_USERNAME=
QNAP_PASSWORD=
source env.ini
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


# cd cgu-manifests
kubectl delete svc -n istio-system istio-ingressgateway
while ! kustomize build ./example | kubectl apply -f -; do echo "Retrying to apply resources"; sleep 20; done
kubectl patch svc -n istio-system istio-ingressgateway -p "{\"spec\":{\"externalIPs\":[\"${MASTER_IP}\"]}}"
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
