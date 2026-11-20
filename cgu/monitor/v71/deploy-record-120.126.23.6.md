# 120.126.23.6 prom-app v71 deployment note

Date: 2026-05-13

## Goal

Make `120.126.23.6` match the monitor behavior on `120.126.23.25` by adding the v71 exporter alongside the existing v53 `prom-app`.

The original v53 deployment remains in place:

- `deployment/node-resource-monitor`
- `service/prom-app`
- image: `markjiang10/cgu-monitor:v53`

The added v71 deployment provides the time metrics used by the frontend Home page:

- `namespace_cpu_cost_time`
- `namespace_gpu_cost_time`

## Source Files

The v71 monitor files were copied from the v71 source used on `120.126.23.25` into this folder:

- `prom_v2.py`
- `prom_v2.yaml`
- `prom_v2_script.yaml`
- `test_prom_v2.py`
- `validate_prom_v2.py`
- `validation_v71.md`
- `README_v2.md`
- `prom_rules_v2.yaml`
- `kube-prom-stack-retention-180d.yaml`
- `workflow.md`

Core file checksums after copy:

```text
a682e1c58099b9b1b5cb642cbc11375f8b7a3d635cc38eccfc313da92ada3fb4  prom_v2.py
83df33aaf896eb9040618414c9df49352bde74760754a9ae0917d95970026df4  prom_v2.yaml
2e1a122b015fd93c494597a3339e2a28302fe4598d7700c4fb19b79ab1f3ab0a  prom_v2_script.yaml
```

## Applied Manifests

Deployment was done from this directory:

```bash
kubectl apply -f prom_v2_script.yaml
kubectl apply -f prom_v2.yaml
kubectl rollout status deploy/node-resource-monitor-v71 -n cgu --timeout=300s
```

Created Kubernetes resources:

- `configmap/node-resource-monitor-script-v71`
- `configmap/node-resource-monitor-config-v71`
- `deployment/node-resource-monitor-v71`
- `service/prom-app-v71`
- `service/prom-app-v71-nodeport`
- `serviceaccount/prom-service-account-v71`
- `clusterrole/node-access-v71`
- `clusterrolebinding/node-access-binding-v71`

The v71 deployment uses:

- image: `python:3.11-slim`
- env: `EXPORTER_VERSION=v71`
- NodePort: `30071`
- Prometheus service: `prom-app-v71`

## Verification

Kubernetes:

```bash
kubectl get deploy,svc,pod -n cgu | grep -E 'node-resource-monitor|prom-app|v71'
```

Expected:

- `deployment/node-resource-monitor` remains ready
- `deployment/node-resource-monitor-v71` is `1/1`
- `pod/node-resource-monitor-v71-...` is `Running`
- `service/prom-app-v71` exists
- `service/prom-app-v71-nodeport` exposes `8080:30071`

Prometheus checks:

```bash
curl -sS --get 'http://120.126.23.6:32049/api/v1/query' \
  --data-urlencode 'query=up{service="prom-app-v71"}'

curl -sS --get 'http://120.126.23.6:32049/api/v1/query' \
  --data-urlencode 'query=cgu_monitor_build_info{service="prom-app-v71"}'

curl -sS --get 'http://120.126.23.6:32049/api/v1/query' \
  --data-urlencode 'query=namespace_cpu_cost_time{service="prom-app-v71",exported_namespace=~".*"}'

curl -sS --get 'http://120.126.23.6:32049/api/v1/query' \
  --data-urlencode 'query=namespace_gpu_cost_time{service="prom-app-v71",exported_namespace=~".*"}'
```

Verified results on 2026-05-13:

- `up{service="prom-app-v71"} = 1`
- `cgu_monitor_build_info{service="prom-app-v71"}` returns `exporter_version="v71"`
- `namespace_cpu_cost_time` returns data
- `namespace_gpu_cost_time` returns data
- `up{service=~".*v70.*"}` returns no results

## Frontend Impact

The Home page already queries:

- `namespace_cpu_cost_time`
- `namespace_gpu_cost_time`

Before v71 was deployed on `120.126.23.6`, those metrics did not exist, so the Home page showed `無資料` for:

- 時間增量
- 時間最大值
- 時間最小值

After v71 deployment, those metrics are available through Prometheus on port `32049`.
