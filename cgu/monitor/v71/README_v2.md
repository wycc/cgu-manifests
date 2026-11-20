# CGU Monitor v71 說明

這版 exporter 的目標不是直接取代舊版，而是先把監控資料拆成兩條線，並保留舊前端相容性。

## 這版做了什麼

- 保留舊指標：
  - `node_cpu_allocatable`
  - `node_gpu_allocatable`
  - `node_memory_allocatable`
  - `node_usage_cpu`
  - `node_usage_gpu`
  - `namespace_usage_cpu`
  - `namespace_usage_gpu`
  - `notebook_cpu_usage`
  - `notebook_memory_usage`
  - `notebook_gpu_usage`
  - `namespace_cpu_cost`
  - `namespace_cpu_cost_time`
  - `namespace_gpu_cost`
  - `namespace_gpu_cost_time`
- 新增新的 `cgu_*` 指標，明確區分：
  - `user_namespace`
  - `kubernetes_node`
  - `notebook_name`
  - `billable usage`
  - `quota`
- 舊指標走舊邏輯相容層，新指標走新語意邏輯，兩條資料線可以同時存在。

## 兩條資料線的差別

### 1. 舊相容層

用途：

- 不改壞現有前端
- 可以直接和目前 `30044` 的舊 exporter 做比對
- `namespace_cpu_cost` / `namespace_gpu_cost` 可以沿用舊累積值

特性：

- `notebook_*` 仍然沿用舊規則，只要 pod 名稱符合 `-數字` 就會被統計
- 這層可能會把 `openldap-0`、`prometheus-0` 這種舊邏輯會統計到的 pod 一起算進去
- 主要用途是「相容」與「平滑切換」，不是未來主要語意

### 2. 新語意層

用途：

- 後續 Account Manager / DB / 新圖表應該優先吃這條

特性：

- 只統計 `Profile` 對應的使用者 namespace
- notebook 只認明確 label，不再用 pod 名稱猜
- `usage` 代表 request / reserved，也就是 billable usage
- `quota` 直接來自 `Profile.spec.resourceQuotaSpec`

## 重要指標

### 舊相容指標

- `namespace_cpu_cost`
- `namespace_cpu_cost_time`
- `namespace_gpu_cost`
- `namespace_gpu_cost_time`

這四個是目前前端 `Home.js` / `User.js` 會直接吃的。

### 新語意指標

- `cgu_namespace_billable_cpu_cores`
- `cgu_namespace_billable_memory_gib`
- `cgu_namespace_billable_gpu_cards`
- `cgu_namespace_quota_cpu_cores`
- `cgu_namespace_quota_memory_gib`
- `cgu_namespace_quota_gpu_cards`
- `cgu_namespace_workload_count`
- `cgu_namespace_notebook_count`
- `cgu_namespace_billable_total_cost_per_minute`
- `cgu_workload_billable_cpu_cores`
- `cgu_notebook_billable_cpu_cores`

## 驗證方式

先跑本地驗證：

```bash
cd /home/mark/work/cgu-kubeflow/cgu/monitor
python3 -m py_compile prom_v2.py test_prom_v2.py validate_prom_v2.py
python3 -m unittest test_prom_v2.py
python3 validate_prom_v2.py --old-url http://120.126.23.25:30044/metrics
```

部署 v71 後，再加上新 exporter endpoint 比對：

```bash
python3 validate_prom_v2.py \
  --old-url http://120.126.23.25:30044/metrics \
  --new-url http://120.126.23.25:30071/metrics \
  --namespace b1144209
```

## 部署

### 1. 重新產生 script ConfigMap YAML

```bash
cd /home/mark/work/cgu-kubeflow/cgu/monitor
kubectl create configmap node-resource-monitor-script-v71 \
  --from-file=prom_v2.py \
  --namespace=cgu \
  --dry-run=client -o yaml > prom_v2_script.yaml
```

### 2. 套用 manifest

```bash
kubectl apply -f /home/mark/work/cgu-kubeflow/cgu/monitor/prom_v2_script.yaml
kubectl apply -f /home/mark/work/cgu-kubeflow/cgu/monitor/prom_v2.yaml
```

### 3. 驗證 NodePort

```bash
curl http://120.126.23.25:30071/metrics
```

## Prometheus 歷史保存

目前 `v71` 的 raw metrics 入口有兩個：

- 舊版 exporter：`http://120.126.23.25:30044/metrics`
- 新版 exporter：`http://120.126.23.25:30071/metrics`

Prometheus 查詢入口則是：

- `http://120.126.23.25:32049`

如果需求是「資料只存在 Prometheus」，正確做法不是另外加 DB 或外部 time-series 系統，而是讓 Prometheus 自己的 TSDB 掛到 PVC。這樣仍然是 Prometheus 本地儲存，只是從 `emptyDir` 改成可持久化。

目前 live cluster 已確認：

- Prometheus retention：`180d`
- Prometheus PVC：`50Gi`
- StorageClass：`nfs-client`
- Prometheus Ready：`http://120.126.23.25:32049/-/ready`

repo 內保留了一份對應的 override 檔：

- `kube-prom-stack-retention-180d.yaml`

可用於後續 Helm 升級或重套時套回相同設定。

## 注意事項

- 這版 `namespace_cpu_cost` / `namespace_gpu_cost` 會先嘗試從舊 exporter 讀 baseline，再往後累加。
- `namespace_cpu_cost_time` / `namespace_gpu_cost_time` 如果舊版沒有提供，會用 `cost / 單價` 先回推出初始值。
- 目前 Prometheus Operator 因為 storage class 問題沒有完全 reconcile，recording rules 先不要在這版硬上，後續流程寫在 `workflow.md`。
- 舊相容 metrics 進 Prometheus 後，如果 sample 本身也有 `namespace` label，常會被 Prometheus 改寫成 `exported_namespace`。前端查 `namespace_cpu_cost` 這類舊指標時，要同時容忍 `namespace` 和 `exported_namespace`。
