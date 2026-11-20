import argparse
import sys
import urllib.request
from typing import Dict, Iterable, Tuple

from kubernetes import client

import prom_v2 as prom


LabelKey = Tuple[Tuple[str, str], ...]
MetricMap = Dict[str, Dict[LabelKey, float]]


LEGACY_METRICS = (
    "node_cpu_allocatable",
    "node_gpu_allocatable",
    "node_memory_allocatable",
    "node_usage_cpu",
    "node_usage_gpu",
    "namespace_usage_cpu",
    "namespace_usage_gpu",
    "notebook_cpu_usage",
    "notebook_memory_usage",
    "notebook_gpu_usage",
    "namespace_cpu_cost",
    "namespace_cpu_cost_time",
    "namespace_gpu_cost",
    "namespace_gpu_cost_time",
)

SEMANTIC_METRICS = (
    "cgu_node_allocatable_cpu_cores",
    "cgu_node_allocatable_memory_gib",
    "cgu_node_allocatable_gpu_cards",
    "cgu_node_billable_cpu_cores",
    "cgu_node_billable_memory_gib",
    "cgu_node_billable_gpu_cards",
    "cgu_namespace_billable_cpu_cores",
    "cgu_namespace_billable_memory_gib",
    "cgu_namespace_billable_gpu_cards",
    "cgu_namespace_workload_count",
    "cgu_namespace_notebook_count",
    "cgu_namespace_quota_cpu_cores",
    "cgu_namespace_quota_memory_gib",
    "cgu_namespace_quota_gpu_cards",
    "cgu_namespace_billable_cpu_cost_per_minute",
    "cgu_namespace_billable_gpu_cost_per_minute",
    "cgu_namespace_billable_total_cost_per_minute",
    "cgu_workload_billable_cpu_cores",
    "cgu_workload_billable_memory_gib",
    "cgu_workload_billable_gpu_cards",
    "cgu_notebook_billable_cpu_cores",
    "cgu_notebook_billable_memory_gib",
    "cgu_notebook_billable_gpu_cards",
)


def label_key(**labels: str) -> LabelKey:
    return tuple(sorted((key, str(value)) for key, value in labels.items()))


def fetch_metrics_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


def build_expected_series(snapshot: Dict[str, Dict]) -> MetricMap:
    expected: MetricMap = {}
    semantic = snapshot["semantic"]
    legacy = snapshot["legacy"]

    def add(metric_name: str, labels: LabelKey, value: float) -> None:
        expected.setdefault(metric_name, {})[labels] = float(value)

    for node_name, value in semantic["node_allocatable_cpu"].items():
        add("cgu_node_allocatable_cpu_cores", label_key(kubernetes_node=node_name), value)
    for node_name, value in semantic["node_allocatable_memory"].items():
        add("cgu_node_allocatable_memory_gib", label_key(kubernetes_node=node_name), value)
    for (node_name, gpu_resource), value in semantic["node_allocatable_gpu"].items():
        add(
            "cgu_node_allocatable_gpu_cards",
            label_key(kubernetes_node=node_name, gpu_resource=gpu_resource),
            value,
        )
    for node_name, value in semantic["node_billable_cpu"].items():
        add("cgu_node_billable_cpu_cores", label_key(kubernetes_node=node_name), value)
    for node_name, value in semantic["node_billable_memory"].items():
        add("cgu_node_billable_memory_gib", label_key(kubernetes_node=node_name), value)
    for (node_name, gpu_resource), value in semantic["node_billable_gpu"].items():
        add(
            "cgu_node_billable_gpu_cards",
            label_key(kubernetes_node=node_name, gpu_resource=gpu_resource),
            value,
        )
    for user_namespace, value in semantic["namespace_billable_cpu"].items():
        add("cgu_namespace_billable_cpu_cores", label_key(user_namespace=user_namespace), value)
    for user_namespace, value in semantic["namespace_billable_memory"].items():
        add("cgu_namespace_billable_memory_gib", label_key(user_namespace=user_namespace), value)
    for (user_namespace, gpu_resource), value in semantic["namespace_billable_gpu"].items():
        add(
            "cgu_namespace_billable_gpu_cards",
            label_key(user_namespace=user_namespace, gpu_resource=gpu_resource),
            value,
        )

    all_user_namespaces = set(snapshot["profile_namespaces"])
    all_user_namespaces.update(semantic["namespace_workload_count"].keys())
    all_user_namespaces.update(semantic["namespace_notebook_count"].keys())
    all_user_namespaces.update(semantic["namespace_quota_cpu"].keys())
    all_user_namespaces.update(semantic["namespace_billable_cpu_cost_rate"].keys())

    for user_namespace in all_user_namespaces:
        labels = label_key(user_namespace=user_namespace)
        add(
            "cgu_namespace_workload_count",
            labels,
            semantic["namespace_workload_count"].get(user_namespace, 0),
        )
        add(
            "cgu_namespace_notebook_count",
            labels,
            semantic["namespace_notebook_count"].get(user_namespace, 0),
        )
        add(
            "cgu_namespace_quota_cpu_cores",
            labels,
            semantic["namespace_quota_cpu"].get(user_namespace, 0.0),
        )
        add(
            "cgu_namespace_quota_memory_gib",
            labels,
            semantic["namespace_quota_memory"].get(user_namespace, 0.0),
        )
        add(
            "cgu_namespace_billable_cpu_cost_per_minute",
            labels,
            semantic["namespace_billable_cpu_cost_rate"].get(user_namespace, 0.0),
        )
        add(
            "cgu_namespace_billable_gpu_cost_per_minute",
            labels,
            semantic["namespace_billable_gpu_cost_rate"].get(user_namespace, 0.0),
        )
        add(
            "cgu_namespace_billable_total_cost_per_minute",
            labels,
            semantic["namespace_billable_total_cost_rate"].get(user_namespace, 0.0),
        )

    for (user_namespace, gpu_resource), value in semantic["namespace_quota_gpu"].items():
        add(
            "cgu_namespace_quota_gpu_cards",
            label_key(user_namespace=user_namespace, gpu_resource=gpu_resource),
            value,
        )

    for labels, value in semantic["workload_billable_cpu"].items():
        user_namespace, kubernetes_node, workload_name, workload_kind, workload_type = labels
        add(
            "cgu_workload_billable_cpu_cores",
            label_key(
                user_namespace=user_namespace,
                kubernetes_node=kubernetes_node,
                workload_name=workload_name,
                workload_kind=workload_kind,
                workload_type=workload_type,
            ),
            value,
        )
    for labels, value in semantic["workload_billable_memory"].items():
        user_namespace, kubernetes_node, workload_name, workload_kind, workload_type = labels
        add(
            "cgu_workload_billable_memory_gib",
            label_key(
                user_namespace=user_namespace,
                kubernetes_node=kubernetes_node,
                workload_name=workload_name,
                workload_kind=workload_kind,
                workload_type=workload_type,
            ),
            value,
        )
    for labels, value in semantic["workload_billable_gpu"].items():
        (
            user_namespace,
            kubernetes_node,
            workload_name,
            workload_kind,
            workload_type,
            gpu_resource,
        ) = labels
        add(
            "cgu_workload_billable_gpu_cards",
            label_key(
                user_namespace=user_namespace,
                kubernetes_node=kubernetes_node,
                workload_name=workload_name,
                workload_kind=workload_kind,
                workload_type=workload_type,
                gpu_resource=gpu_resource,
            ),
            value,
        )
    for labels, value in semantic["notebook_billable_cpu"].items():
        user_namespace, kubernetes_node, notebook_name = labels
        add(
            "cgu_notebook_billable_cpu_cores",
            label_key(
                user_namespace=user_namespace,
                kubernetes_node=kubernetes_node,
                notebook_name=notebook_name,
            ),
            value,
        )
    for labels, value in semantic["notebook_billable_memory"].items():
        user_namespace, kubernetes_node, notebook_name = labels
        add(
            "cgu_notebook_billable_memory_gib",
            label_key(
                user_namespace=user_namespace,
                kubernetes_node=kubernetes_node,
                notebook_name=notebook_name,
            ),
            value,
        )
    for labels, value in semantic["notebook_billable_gpu"].items():
        user_namespace, kubernetes_node, notebook_name, gpu_resource = labels
        add(
            "cgu_notebook_billable_gpu_cards",
            label_key(
                user_namespace=user_namespace,
                kubernetes_node=kubernetes_node,
                notebook_name=notebook_name,
                gpu_resource=gpu_resource,
            ),
            value,
        )

    for node_name, value in legacy["node_cpu_allocatable"].items():
        add("node_cpu_allocatable", label_key(node=node_name), value)
    for node_name, value in legacy["node_gpu_allocatable"].items():
        add("node_gpu_allocatable", label_key(node=node_name), value)
    for node_name, value in legacy["node_memory_allocatable"].items():
        add("node_memory_allocatable", label_key(node=node_name), value)
    for node_name, value in legacy["node_usage_cpu"].items():
        add("node_usage_cpu", label_key(node=node_name), value)
    for node_name, value in legacy["node_usage_gpu"].items():
        add("node_usage_gpu", label_key(node=node_name), value)
    for namespace_name, value in legacy["namespace_usage_cpu"].items():
        add("namespace_usage_cpu", label_key(namespace=namespace_name), value)
    for namespace_name, value in legacy["namespace_usage_gpu"].items():
        add("namespace_usage_gpu", label_key(namespace=namespace_name), value)
    for labels, value in legacy["notebook_cpu_usage"].items():
        node_name, namespace_name, notebook_name = labels
        add(
            "notebook_cpu_usage",
            label_key(node=node_name, namespace=namespace_name, notebook=notebook_name),
            value,
        )
    for labels, value in legacy["notebook_memory_usage"].items():
        node_name, namespace_name, notebook_name = labels
        add(
            "notebook_memory_usage",
            label_key(node=node_name, namespace=namespace_name, notebook=notebook_name),
            value,
        )
    for labels, value in legacy["notebook_gpu_usage"].items():
        node_name, namespace_name, notebook_name = labels
        add(
            "notebook_gpu_usage",
            label_key(node=node_name, namespace=namespace_name, notebook=notebook_name),
            value,
        )
    for namespace_name, value in legacy["namespace_cpu_cost"].items():
        add("namespace_cpu_cost", label_key(namespace=namespace_name), value)
    for namespace_name, value in legacy["namespace_cpu_cost_time"].items():
        add("namespace_cpu_cost_time", label_key(namespace=namespace_name), value)
    for namespace_name, value in legacy["namespace_gpu_cost"].items():
        add("namespace_gpu_cost", label_key(namespace=namespace_name), value)
    for namespace_name, value in legacy["namespace_gpu_cost_time"].items():
        add("namespace_gpu_cost_time", label_key(namespace=namespace_name), value)

    return expected


def compare_metric(
    metric_name: str,
    expected: Dict[LabelKey, float],
    actual: Dict[LabelKey, float],
    tolerance: float,
    max_report: int,
) -> Tuple[bool, Iterable[str]]:
    ok = True
    lines = []

    for labels in sorted(set(expected) | set(actual)):
        expected_value = expected.get(labels)
        actual_value = actual.get(labels)
        if expected_value is None:
            ok = False
            lines.append(f"  extra   {metric_name}{dict(labels)} = {actual_value}")
            continue
        if actual_value is None:
            ok = False
            lines.append(f"  missing {metric_name}{dict(labels)} expected={expected_value}")
            continue
        if abs(expected_value - actual_value) > tolerance:
            ok = False
            lines.append(
                f"  diff    {metric_name}{dict(labels)} expected={expected_value} actual={actual_value}"
            )

    if len(lines) > max_report:
        lines = list(lines[:max_report]) + [
            f"  ... {len(lines) - max_report} more differences omitted"
        ]
    return ok, lines


def compare_overlap_metric(
    metric_name: str,
    expected: Dict[LabelKey, float],
    actual: Dict[LabelKey, float],
    tolerance: float,
    max_report: int,
) -> Tuple[bool, str, Iterable[str]]:
    if not actual:
        return True, "SKIP", [f"  old exporter did not expose {metric_name}"]

    ok = True
    lines = []
    for labels, actual_value in sorted(actual.items()):
        expected_value = expected.get(labels)
        if expected_value is None:
            if abs(actual_value) <= tolerance:
                lines.append(f"  note    stale-zero {metric_name}{dict(labels)} = {actual_value}")
                continue
            ok = False
            lines.append(f"  extra   {metric_name}{dict(labels)} = {actual_value}")
            continue
        if abs(expected_value - actual_value) > tolerance:
            ok = False
            lines.append(
                f"  diff    {metric_name}{dict(labels)} expected={expected_value} actual={actual_value}"
            )

    expected_missing = [
        f"  note    expected-only {metric_name}{dict(labels)} = {expected_value}"
        for labels, expected_value in sorted(expected.items())
        if labels not in actual and abs(expected_value) > tolerance
    ]
    if expected_missing:
        lines.extend(expected_missing)

    if len(lines) > max_report:
        lines = list(lines[:max_report]) + [
            f"  ... {len(lines) - max_report} more differences omitted"
        ]
    return ok, ("OK" if ok else "FAIL"), lines


def print_focus_namespace(metric_map: MetricMap, namespace: str) -> None:
    print(f"\n[focus namespace] {namespace}")
    for metric_name in sorted(metric_map):
        matches = []
        for labels, value in metric_map[metric_name].items():
            label_dict = dict(labels)
            if label_dict.get("namespace") == namespace or label_dict.get("user_namespace") == namespace:
                matches.append((label_dict, value))
        if not matches:
            continue
        print(metric_name)
        for label_dict, value in sorted(matches, key=lambda item: sorted(item[0].items())):
            print(f"  {label_dict} = {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate prom_v2 expected values against old/new exporters.")
    parser.add_argument("--old-url", default="http://120.126.23.25:30044/metrics")
    parser.add_argument("--new-url", default="")
    parser.add_argument("--namespace", default="b1144209")
    parser.add_argument("--tolerance", type=float, default=1e-6)
    parser.add_argument("--cost-tolerance", type=float, default=3.0)
    parser.add_argument("--max-report", type=int, default=20)
    args = parser.parse_args()

    prom.load_kubernetes_config()
    core_v1 = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()
    cpu_cost_per_minute, gpu_cost_per_minute = prom.current_cost_settings()
    nodes, namespaces, pods, profiles = prom.collect_cluster_state(core_v1, custom_api)

    snapshot = prom.build_snapshot(
        nodes=nodes,
        namespaces=namespaces,
        pods=pods,
        profiles=profiles,
        cpu_cost_per_minute=cpu_cost_per_minute,
        gpu_cost_per_minute=gpu_cost_per_minute,
        legacy_cost_state=prom.LegacyCostState(),
        now_monotonic=0.0,
    )
    expected = build_expected_series(snapshot)

    old_text = fetch_metrics_text(args.old_url)
    old_actual = prom.fetch_metric_samples_from_text(old_text, set(LEGACY_METRICS))

    print("[legacy validation] K8s expected/bootstrapped vs old 30044 exporter")
    legacy_ok = True
    for metric_name in LEGACY_METRICS:
        tolerance = args.cost_tolerance if "_cost" in metric_name else args.tolerance
        ok, status, lines = compare_overlap_metric(
            metric_name,
            expected.get(metric_name, {}),
            old_actual.get(metric_name, {}),
            tolerance,
            args.max_report,
        )
        print(f"- {metric_name}: {status}")
        for line in lines:
            print(line)
        legacy_ok = legacy_ok and ok

    new_ok = True
    if args.new_url:
        new_text = fetch_metrics_text(args.new_url)
        new_actual = prom.fetch_metric_samples_from_text(
            new_text,
            set(LEGACY_METRICS) | set(SEMANTIC_METRICS),
        )
        print("\n[new exporter validation] K8s expected vs prom_v2 endpoint")
        for metric_name in LEGACY_METRICS + SEMANTIC_METRICS:
            tolerance = args.cost_tolerance if "_cost" in metric_name else args.tolerance
            ok, lines = compare_metric(
                metric_name,
                expected.get(metric_name, {}),
                new_actual.get(metric_name, {}),
                tolerance,
                args.max_report,
            )
            status = "OK" if ok else "FAIL"
            print(f"- {metric_name}: {status}")
            for line in lines:
                print(line)
            new_ok = new_ok and ok
        print_focus_namespace(new_actual, args.namespace)
    else:
        print("\n[new exporter validation] skipped: provide --new-url after v71 is exposed.")

    print_focus_namespace(expected, args.namespace)
    return 0 if legacy_ok and new_ok else 1


if __name__ == "__main__":
    sys.exit(main())
