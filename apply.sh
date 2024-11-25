kubectl delete svc -n istio-system istio-ingressgateway
while ! kustomize build example | kubectl apply -f -; do echo "Retrying to apply resources"; sleep 20; done
kubectl patch svc -n istio-system istio-ingressgateway -p '{"spec":{"externalIPs":["120.126.23.25"]}}'
# kubectl patch svc -n istio-system istio-ingressgateway -p '{"spec":{"type":"NodePort"}}'
