# Monitor v71 驗證紀錄

## 驗證目標

1. 新 exporter 不能直接憑感覺部署，必須先對 K8s 真實資料。
2. 新 exporter 必須保留舊前端需要的指標。
3. 新 exporter 需要補上新的 `cgu_*` 指標，供後續 Account Manager / DB 使用。

## 本次部署資訊

- 版本標記：`v71`
- 內部 service：`prom-app-v71`
- 外部 NodePort：`120.126.23.25:30071`
- 舊正式版 NodePort：`120.126.23.25:30044`

## 驗證方式

### 1. 單元測試

執行：

```bash
cd /home/mark/work/cgu-kubeflow/cgu/monitor
python3 -m unittest test_prom_v2.py
```

結果：

- `15` 個測試全過

### 2. 舊 exporter overlap 比對

執行：

```bash
python3 validate_prom_v2.py \
  --old-url http://120.126.23.25:30044/metrics \
  --namespace b1144209
```

結果摘要：

- `node_cpu_allocatable`：OK
- `node_gpu_allocatable`：OK
- `node_memory_allocatable`：OK
- `namespace_cpu_cost`：OK
- `namespace_gpu_cost`：OK
- `notebook_cpu_usage`：OK
- `notebook_memory_usage`：OK
- `notebook_gpu_usage`：OK

注意：

- 舊 exporter 沒有穩定提供：
  - `node_usage_cpu`
  - `node_usage_gpu`
  - `namespace_usage_cpu`
  - `namespace_usage_gpu`
  - `namespace_cpu_cost_time`
  - `namespace_gpu_cost_time`
- 舊 exporter 還會殘留一些 `0` 值 notebook 系列，屬於 stale series，不列為失敗。

### 3. 新 exporter full match 比對

執行：

```bash
python3 validate_prom_v2.py \
  --old-url http://120.126.23.25:30044/metrics \
  --new-url http://120.126.23.25:30071/metrics \
  --namespace b1144209
```

結果：

- 舊相容 metrics：全部 OK
- 新 `cgu_*` metrics：全部 OK

## 實際 namespace 驗證

### `b1144209`

NodePort 查詢：

```bash
curl http://120.126.23.25:30071/metrics
```

確認：

- `cgu_namespace_quota_cpu_cores{user_namespace="b1144209"} = 8.8`
- `namespace_cpu_cost{namespace="b1144209"} = 793.8000000000021`
- `namespace_cpu_cost_time{namespace="b1144209"} = 396.90000000000106`

說明：

- `b1144209` 目前 quota 有值
- 目前沒有正在跑的使用者 workload，所以 billable usage 為 `0`

### `b1128005`

K8s pod request：

- pod：`notebook1-0`
- node：`dev27`
- CPU request：`500m + 100m = 0.6`
- GPU request：`1`

新 exporter 回報：

- `cgu_namespace_billable_cpu_cores{user_namespace="b1128005"} = 0.6`
- `cgu_namespace_billable_gpu_cards{user_namespace="b1128005",gpu_resource="nvidia.com/gpu"} = 1`
- `cgu_notebook_billable_cpu_cores{user_namespace="b1128005",notebook_name="notebook1"} = 0.6`
- `cgu_notebook_billable_gpu_cards{user_namespace="b1128005",notebook_name="notebook1",gpu_resource="nvidia.com/gpu"} = 1`
- `namespace_usage_cpu{namespace="b1128005"} = 0.6`

結論：

- `v71` 對 request / reserved 的統計和 K8s 真實資料一致

## Prometheus 驗證

查詢：

```bash
curl 'http://120.126.23.25:32049/api/v1/query?query=up{service="prom-app-v71"}'
curl 'http://120.126.23.25:32049/api/v1/query?query=cgu_monitor_build_info{service="prom-app-v71",exporter_version="v71"}'
```

結果：

- `up{service="prom-app-v71"} = 1`
- `cgu_monitor_build_info{service="prom-app-v71",exporter_version="v71"} = 1`

注意：

- 舊相容 metrics 如果 label 叫 `namespace`，進 Prometheus 後通常會被改寫成 `exported_namespace`
- 新 `cgu_*` 指標直接使用 `user_namespace`，不會踩到這個問題

## 本次結論

- `v71` 可以平行提供舊相容指標與新語意指標
- 已經通過：
  - 本地測試
  - 舊 exporter overlap 驗證
  - K8s 真實資料 full match 驗證
  - Prometheus scrape 驗證
- 目前可以用 `30071` 當新版本觀察入口
- 暫時不需要先改前端 UI 結構，也不需要先碰 recording rules
