# Monitor v71 後續流程

## 目前先做的事情

1. 先把 `prom_v2.py` 做成 `v71`，同時提供：
   - 舊前端相容指標
   - 新的 `cgu_*` 語意指標
2. 先驗證：
   - 舊 `30044` exporter 數值是否能對上
   - K8s API 算出的 request / quota 是否能對上新 exporter
3. 再讓 Account Manager / DB 逐步從 Prometheus 拿新指標

## Prometheus 目前狀態

目前 live cluster 已處理成：

- retention：`180d`
- storage：PVC `50Gi`
- storageClass：`nfs-client`
- Prometheus 查詢入口：`http://120.126.23.25:32049`

這仍然是「資料存在 Prometheus」，不是額外接外部儲存。差別只是 Prometheus TSDB 不再放在 `emptyDir`，而是放在 PVC 上，避免 pod 重建就把歷史資料清掉。

repo 內對應檔案：

- `kube-prom-stack-retention-180d.yaml`

## 為什麼 recording rules 先延後

原本 Prometheus Operator 確實卡過這個問題：

- `storageClassName: standard` 不存在
- operator 曾經 `ReconciliationFailed`

這一塊已先改成 `nfs-client + PVC`，Prometheus 本體目前已恢復 Ready。  
但 `recording rules` 這次仍然先不一起上，原因是這一輪先聚焦在：

1. 先讓 `v71` exporter 穩定提供正確 raw metrics
2. 先把歷史保存從 `10d + emptyDir` 改成 `180d + PVC`
3. 再處理後續 query 聚合與前端正式切換

## recording rules 之後要做什麼

等 Prometheus storage / operator 修好後，再做下面這些：

1. 把 `cgu_namespace_billable_*` 做 hourly / daily 聚合
2. 把 `cgu_namespace_billable_total_cost_per_minute` 做累積查詢模板
3. 讓 Account Manager 優先查 recording rules，不直接在前端寫複雜 PromQL
4. 後續如果要做 DB 結算，再由定時 job 從 recording rules 或 Prometheus API 寫入資料庫

## 建議順序

1. 先確認 `30071` 新 exporter 穩定
2. 再讓前端加欄位或切查詢
3. 最後才做 recording rules 和 DB 定時彙總
