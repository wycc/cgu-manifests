# CGU Monitor v71（計費 exporter）部署說明

v71 exporter 依每個使用者 namespace 的 CPU / GPU request 計算用量與費用，提供給 AI_Centre_Admin 前端（Home / User 頁）使用。
舊版 v53（`markjiang10/cgu-monitor:v53`）已於 2026-11-20 停用，由 v71 取代；還在跑 v53 的叢集照下方「移除 v53」處理。

指標與設計說明見 [README_v2.md](README_v2.md)，與 v53 的數值比對見 [validation_v71.md](validation_v71.md)。

## 檔案

| 檔案 | 用途 |
|---|---|
| `prom_v2.py` | exporter 原始碼 |
| `kustomization.yaml` | 把下列資源組起來，並從 `prom_v2.py` 產生 ConfigMap `node-resource-monitor-script-v71` |
| `monitor-config.yaml` | 費率 ConfigMap `node-resource-monitor-config`（後端費率 API 需要） |
| `Dockerfile`、`requirements.txt` | monitor image，只有套件 |
| `prom_v2.yaml` | 費率 ConfigMap、Deployment、Service（含 NodePort 30071）、ServiceAccount、RBAC |
| `prom_rules_v2.yaml` | recording rules，與 .6 線上相同 |
| `apply-prometheus-settings.sh` | 設定 Prometheus 的 NodePort 32049 與保留 360 天（.6 目前維持 180 天，不要在 .6 加 `CONFIRM_DATA_LOSS=yes` 執行） |
| `servicemonitor-v71.yaml` | Prometheus 抓取 v71 用的 ServiceMonitor（已包含在 `kustomization.yaml`） |
| `kube-prom-stack-retention-180d.yaml` | .25 用過的 helm values。**.4、.6 不要用**：50Gi 放不下 180 天的資料，而且 Prometheus 官方不支援 NFS |
| `test_prom_v2.py` | 單元測試 |
| `validate_prom_v2.py` | 比對 v53 與 v71 `/metrics` 的工具 |
| `deploy-record-120.126.23.6.md` | 2026-05-13 部署到 .6 的紀錄 |

## 部署

前提：叢集已有 `cgu` namespace、kube-prometheus-stack（ServiceMonitor 需帶 `release: kube-prom-stack`）、Kubeflow `Profile` CRD。
Pod 用 `docker.io/cguaicadmin/cgu-monitor-v71`，image 只裝好固定版本的套件，啟動不需要連 PyPI。程式 `prom_v2.py` 放在 ConfigMap `node-resource-monitor-script-v71`，改程式只要重新 apply 並重啟 pod，不用重建 image。

只有改 `requirements.txt` 或 Python 版本時才要重建 image（在 `cgu/monitor/v71` 執行，tag 用新的時間）：

```bash
docker build -t docker.io/cguaicadmin/cgu-monitor-v71:vYYMMDDHHMM .
docker push docker.io/cguaicadmin/cgu-monitor-v71:vYYMMDDHHMM
```

之後把 `prom_v2.yaml` 的 `image:` 改成新的 tag。

在 repo 根目錄執行。先看差異，確認後才把 `kubectl diff` 換成 `kubectl apply`：

```bash
kustomize build cgu/monitor/v71 | python3 preserve_live_state.py | kubectl diff -f -
kustomize build cgu/monitor/v71 | python3 preserve_live_state.py | kubectl apply -f -
kubectl rollout status deploy/node-resource-monitor-v71 -n cgu --timeout=300s
bash cgu/monitor/v71/apply-prometheus-settings.sh
```

- 一定要經過 `preserve_live_state.py`，否則會把網頁上改過的費率蓋回初始值。
- v71 不再依賴 v53：抓取用自己的 ServiceMonitor，累積費用從 Prometheus 接續（見下方「累積費用」）。
- Prometheus 沒有 PVC 時，改保留天數會重建 pod 並清掉所有資料，腳本會先停下來；確認可接受後加 `CONFIRM_DATA_LOSS=yes` 再跑。
- 完整的 `apply.sh` 也會裝 v71（`cgu/monitor/kustomization.yaml` 已包含），但它還會動到其他元件，只更新 monitor 時不要用。

### 累積費用（`namespace_*_cost*`）

這四個累積值只存在 exporter 記憶體裡。啟動時 v71 會向本叢集的 Prometheus（`LEGACY_BOOTSTRAP_PROMETHEUS_URL`）查自己過去 30 天的最高值接著累積：

```
max by (exported_namespace) (max_over_time(namespace_cpu_cost{service="prom-app-v71"}[30d]))
```

- 累積值只增不減，最高值就是重啟前最後的值；舊 pod 的 series 被標成 stale 也查得到。
- 查詢時間固定為 pod 啟動的那一刻（Prometheus API 的 `time` 參數），只用啟動前的資料。
- Prometheus 暫時連不上時，每分鐘重試；期間先從 0 累積，接上後加在查到的值上，不會覆蓋，也不會把這段期間被抓走的值重複算一次。
- 有 pod 或 Profile 的資源寫法認不得時，log 會出現一次 `Ignoring unparsable resource quantity`，該值當成 0，其他 namespace 照常計費。
- 新叢集查不到歷史時從 0 開始，視為成功，不會一直重試。
- `cgu_monitor_legacy_bootstrap_success` 為 1 表示已接續。
- 可調整：`LEGACY_BOOTSTRAP_SERVICE`（預設 `prom-app-v71`）、`LEGACY_BOOTSTRAP_LOOKBACK`（預設 `30d`）、`LEGACY_BOOTSTRAP_TIMEOUT_SECONDS`（預設 30）。
- 沒設 `LEGACY_BOOTSTRAP_PROMETHEUS_URL` 時才使用 `LEGACY_BOOTSTRAP_URL`（某個 `/metrics` 網址，例如舊 v53）；兩個都沒設就從 0 開始。
- Prometheus 沒有 PVC，Prometheus pod 重建時歷史資料連同這些累積值都會消失。

AI_Centre_Admin 首頁的計費（`cgu_namespace_billable_*`）是對 Prometheus 歷史積分，不使用這四個累積值。

### 移除 v53（還在跑 v53 的叢集，例如 2026-11 的 .4、.6）

`kubectl apply` 不會刪掉 repo 已移除的物件，要手動刪。先套用 v71（新增 ServiceMonitor、改接續來源，v71 pod 會重啟一次），確認接續成功後再刪 v53：

```bash
kustomize build cgu/monitor/v71 | python3 preserve_live_state.py | kubectl diff -f -
kustomize build cgu/monitor/v71 | python3 preserve_live_state.py | kubectl apply -f -
kubectl rollout status deploy/node-resource-monitor-v71 -n cgu --timeout=300s
kubectl logs -n cgu deploy/node-resource-monitor-v71 | grep 'Loaded cost bootstrap'   # 應有「Loaded cost bootstrap for N namespaces」

kubectl -n cgu delete servicemonitor node-resource-monitor          # v53 的，selector app=prom-app 也會抓到 v71
kubectl -n cgu delete deploy node-resource-monitor
kubectl -n cgu delete svc prom-app node-resource-monitor
kubectl -n cgu delete sa prom-service-account
kubectl delete clusterrolebinding node-access-binding
kubectl delete clusterrole node-access
```

- 兩個 ServiceMonitor 同時存在的那一兩分鐘，v71 會被抓兩次（同樣的 label），Prometheus 只會記錄 duplicate sample 警告。
- v53 的舊資料留在 Prometheus，到保留期限才消失；刪掉 v53 不會刪歷史。
- 只有 v53 用到的補丁也可刪除，例如 .4 的 ConfigMap `node-resource-monitor-fix`（v53 的 kubernetes 12.0.1 遇到沒有 tag 的節點 image 會 crash；v71 用 26.1.0，沒有這個問題）。

## 費率

`node-resource-monitor-config` 與 `node-resource-monitor-config-v71` 裡的 `CPU_COST_PER_MINUTE: "0.03"`、`GPU_COST_PER_MINUTE: "0.7"` 只是初始值，與 .6 線上設定相同。
上線後費率由 AI_Centre_Admin 管理介面修改（兩個 ConfigMap 會一起改）。
這兩個 ConfigMap 列在 `preserve_live_state.py` 的 `CREATE_ONLY`，已存在時不會被重新套用。直接 `kubectl apply -f prom_v2.yaml` 會繞過這層保護，不要這樣做。

ConfigMap 讀不到時，程式預設值同樣是 0.03 / 0.7（`prom_v2.py` 的 `DEFAULT_*_COST_PER_MINUTE`）。

## 驗證

```bash
kubectl get deploy,svc,pod -n cgu | grep -E 'node-resource-monitor-v71|prom-app-v71'
curl -s http://<node-ip>:30071/metrics | grep -E '^(cgu_monitor_build_info|namespace_(cpu|gpu)_cost_time)' | head
```

Prometheus 上應查得到 `up{service="prom-app-v71"} == 1`，以及 `namespace_cpu_cost_time{service="prom-app-v71"}`。

## 修改程式之後

ConfigMap 會從 `prom_v2.py` 自動產生，但更新它不會重啟 pod，要自己 restart：

```bash
(cd cgu/monitor/v71 && python3 -m unittest test_prom_v2)   # 需要 kubernetes==26.1.0、prometheus-client==0.20.0
kustomize build cgu/monitor/v71 | python3 preserve_live_state.py | kubectl apply -f -
kubectl rollout restart deploy/node-resource-monitor-v71 -n cgu
```

重啟 exporter 不會影響 Prometheus 裡的歷史資料；`namespace_*_cost*` 累積值重啟後會從 Prometheus 接續（見「累積費用」）。`cgu_namespace_billable_*` 不受影響。
