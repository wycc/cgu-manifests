#!/bin/bash
kubectl delete svc -n istio-system istio-ingressgateway
while ! kustomize build example | kubectl apply -f -; do echo "Retrying to apply resources"; sleep 20; done
kubectl patch svc -n istio-system istio-ingressgateway -p '{"spec":{"externalIPs":["120.126.23.25"]}}'
# kubectl patch svc -n istio-system istio-ingressgateway -p '{"spec":{"type":"NodePort"}}'
while ! kustomize build ./example | kubectl apply -f -; do echo "Retrying to apply resources"; sleep 10; done
kubectl taint nodes $(hostname -s) node-role.kubernetes.io/control-plane:NoSchedule
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
helm repo update
VER=0.15.1
helm upgrade -i nvdp nvdp/nvidia-device-plugin \
  --version $VER \
  --namespace nvidia-device-plugin \
  --create-namespace \
  --set compatWithCPUManager=true \
  --set gfd.enabled=true
