import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from typing import DefaultDict, Dict, Iterable, Optional, Set, Tuple

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from prometheus_client import Gauge, start_http_server
from prometheus_client.parser import text_string_to_metric_families


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("cgu-monitor-v71")

SCRAPE_PORT = int(os.getenv("SCRAPE_PORT", "8080"))
UPDATE_INTERVAL_SECONDS = int(os.getenv("UPDATE_INTERVAL_SECONDS", "60"))
EXPORTER_VERSION = os.getenv("EXPORTER_VERSION", "v71")
PROFILE_GROUP = os.getenv("PROFILE_GROUP", "kubeflow.org")
PROFILE_VERSION = os.getenv("PROFILE_VERSION", "v1")
PROFILE_PLURAL = os.getenv("PROFILE_PLURAL", "profiles")
DEFAULT_CPU_COST_PER_MINUTE = float(os.getenv("DEFAULT_CPU_COST_PER_MINUTE", "0.03"))
DEFAULT_GPU_COST_PER_MINUTE = float(os.getenv("DEFAULT_GPU_COST_PER_MINUTE", "0.7"))
COST_CONFIG_PATH = os.getenv("COST_CONFIG_PATH", "/etc/node-resource-monitor")
# Where the cumulative namespace_*_cost* counters are restored from after a restart
# (they live only in memory). Preferred: this cluster's Prometheus, reading back v71's
# own last values. LEGACY_BOOTSTRAP_URL (a /metrics page, e.g. the old v53 exporter)
# is used only when no Prometheus URL is set. Neither set: counters start at 0.
LEGACY_BOOTSTRAP_PROMETHEUS_URL = os.getenv("LEGACY_BOOTSTRAP_PROMETHEUS_URL", "").rstrip("/")
LEGACY_BOOTSTRAP_SERVICE = os.getenv("LEGACY_BOOTSTRAP_SERVICE", "prom-app-v71")
LEGACY_BOOTSTRAP_LOOKBACK = os.getenv("LEGACY_BOOTSTRAP_LOOKBACK", "30d")
LEGACY_BOOTSTRAP_URL = os.getenv("LEGACY_BOOTSTRAP_URL", "")
# Bootstrap reads Prometheus as it was when this process started, so the from-zero
# counters this process exports before a late bootstrap never become the baseline.
PROCESS_START_TIME = time.time()
LEGACY_BOOTSTRAP_TIMEOUT_SECONDS = float(
    os.getenv("LEGACY_BOOTSTRAP_TIMEOUT_SECONDS", "30.0")
)
LEGACY_COST_METRICS = (
    "namespace_cpu_cost",
    "namespace_gpu_cost",
    "namespace_cpu_cost_time",
    "namespace_gpu_cost_time",
)

NOTEBOOK_LABEL_KEYS = (
    "notebook-name",
    "notebooks.kubeflow.org/notebook-name",
)
TERMINAL_PHASES = {"Succeeded", "Failed"}
REPLICASET_HASH_RE = re.compile(r"^(?P<name>.+)-[a-f0-9]{9,10}$")
LEGACY_NOTEBOOK_RE = re.compile(r"-\d+$")


@dataclass
class LegacyCostState:
    cpu_cost: Dict[str, float] = field(default_factory=dict)
    gpu_cost: Dict[str, float] = field(default_factory=dict)
    cpu_cost_time: Dict[str, float] = field(default_factory=dict)
    gpu_cost_time: Dict[str, float] = field(default_factory=dict)
    bootstrapped: bool = False
    last_update_monotonic: Optional[float] = None


def load_kubernetes_config() -> None:
    try:
        config.load_incluster_config()
        LOGGER.info("Loaded in-cluster Kubernetes config.")
    except Exception:
        config.load_kube_config()
        LOGGER.info("Loaded kubeconfig from local environment.")


def read_cost_from_config(key: str, default_value: float) -> float:
    env_value = os.getenv(key)
    if env_value is not None:
        try:
            return float(env_value)
        except ValueError:
            LOGGER.warning("Invalid %s env value %r. Falling back.", key, env_value)

    file_path = os.path.join(COST_CONFIG_PATH, key)
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            return float(handle.read().strip())
    except FileNotFoundError:
        return default_value
    except ValueError:
        LOGGER.warning("Invalid numeric value in %s. Falling back.", file_path)
        return default_value


def current_cost_settings() -> Tuple[float, float]:
    return (
        read_cost_from_config("CPU_COST_PER_MINUTE", DEFAULT_CPU_COST_PER_MINUTE),
        read_cost_from_config("GPU_COST_PER_MINUTE", DEFAULT_GPU_COST_PER_MINUTE),
    )


# Kubernetes quantity syntax: a number, optional exponent, optional suffix.
_QUANTITY_RE = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-z]*)$")
# CPU and other plain quantities, in base units.
_DECIMAL_SUFFIXES = {
    "": 1.0, "n": 1e-9, "u": 1e-6, "m": 1e-3,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
    "Ki": 2.0**10, "Mi": 2.0**20, "Gi": 2.0**30, "Ti": 2.0**40, "Pi": 2.0**50, "Ei": 2.0**60,
}
# Memory in bytes. The decimal letters deliberately count as their binary units
# (1G == 1Gi), as this exporter always has; only the sub-byte suffixes stay decimal.
_MEMORY_SUFFIXES = {
    "": 1.0, "n": 1e-9, "u": 1e-6, "m": 1e-3,
    "k": 2.0**10, "K": 2.0**10, "M": 2.0**20, "G": 2.0**30, "T": 2.0**40, "P": 2.0**50, "E": 2.0**60,
    "Ki": 2.0**10, "Mi": 2.0**20, "Gi": 2.0**30, "Ti": 2.0**40, "Pi": 2.0**50, "Ei": 2.0**60,
}
_WARNED_QUANTITIES: Set[str] = set()


def _parse_quantity(raw: Optional[str], suffixes: Dict[str, float]) -> float:
    """Parse a Kubernetes quantity; an unparsable value counts as 0 (warned once) so one
    odd pod or Profile cannot stop the refresh for every namespace."""
    if raw is None or raw == "":
        return 0.0
    text = str(raw).strip()
    match = _QUANTITY_RE.match(text)
    if match and match.group(2) in suffixes:
        return float(match.group(1)) * suffixes[match.group(2)]
    if text not in _WARNED_QUANTITIES:
        _WARNED_QUANTITIES.add(text)
        LOGGER.warning("Ignoring unparsable resource quantity %r (counted as 0).", text)
    return 0.0


def parse_cpu_quantity(raw: Optional[str]) -> float:
    return _parse_quantity(raw, _DECIMAL_SUFFIXES)


def parse_memory_to_gib(raw: Optional[str]) -> float:
    return _parse_quantity(raw, _MEMORY_SUFFIXES) / 2.0**30


def iter_gpu_resources(resource_map: Optional[Dict[str, str]]) -> Iterable[Tuple[str, float]]:
    if not resource_map:
        return
    for resource_name, quantity in resource_map.items():
        lowered = resource_name.lower()
        if "gpu" not in lowered:
            continue
        try:
            yield resource_name, float(quantity)
        except ValueError:
            LOGGER.warning("Failed to parse GPU quantity %r for %s.", quantity, resource_name)


def identify_notebook_name(pod: client.V1Pod) -> Optional[str]:
    labels = pod.metadata.labels or {}
    for key in NOTEBOOK_LABEL_KEYS:
        if labels.get(key):
            return labels[key]
    return None


def legacy_identify_notebook_name(pod: client.V1Pod) -> Optional[str]:
    name = pod.metadata.name or ""
    if LEGACY_NOTEBOOK_RE.search(name):
        return name
    return None


def parse_profile_namespaces(profiles: Iterable[Dict]) -> Set[str]:
    namespaces: Set[str] = set()
    for profile in profiles:
        metadata = profile.get("metadata", {}) or {}
        name = metadata.get("name")
        if name:
            namespaces.add(name)
    return namespaces


def normalize_replicaset_name(name: str) -> str:
    match = REPLICASET_HASH_RE.match(name or "")
    if match:
        return match.group("name")
    return name


def identify_workload(pod: client.V1Pod) -> Tuple[str, str, str]:
    labels = pod.metadata.labels or {}
    notebook_name = identify_notebook_name(pod)
    if notebook_name:
        return notebook_name, "StatefulSet", "notebook"

    owner_references = pod.metadata.owner_references or []
    controller_owner = next((owner for owner in owner_references if getattr(owner, "controller", False)), None)
    if controller_owner and controller_owner.kind and controller_owner.name:
        owner_kind = controller_owner.kind
        owner_name = controller_owner.name
        if owner_kind == "ReplicaSet":
            workload_name = (
                labels.get("app.kubernetes.io/instance")
                or labels.get("app.kubernetes.io/name")
                or normalize_replicaset_name(owner_name)
            )
            if labels.get("app.kubernetes.io/part-of") == "pvc-viewer":
                return workload_name, "Deployment", "pvcviewer"
            return workload_name, "Deployment", "deployment"
        if owner_kind == "StatefulSet":
            return owner_name, "StatefulSet", "statefulset"
        if owner_kind == "VirtualMachineInstance":
            return owner_name, "VirtualMachineInstance", "vm"
        return owner_name, owner_kind, owner_kind.lower()

    if labels.get("app.kubernetes.io/part-of") == "pvc-viewer":
        workload_name = labels.get("app.kubernetes.io/instance") or pod.metadata.name or "unknown"
        return workload_name, "Deployment", "pvcviewer"

    return pod.metadata.name or "unknown", "Pod", "pod"


def should_track_pod(pod: client.V1Pod, profile_namespaces: Set[str]) -> bool:
    if pod.status and pod.status.phase in TERMINAL_PHASES:
        return False
    return (pod.metadata.namespace or "") in profile_namespaces


def collect_pod_requests(pod: client.V1Pod) -> Tuple[float, float, Dict[str, float]]:
    cpu = 0.0
    memory_gib = 0.0
    gpu_map: DefaultDict[str, float] = defaultdict(float)
    for container_item in pod.spec.containers or []:
        resources = container_item.resources
        requests = resources.requests if resources and resources.requests else {}
        cpu += parse_cpu_quantity(requests.get("cpu"))
        memory_gib += parse_memory_to_gib(requests.get("memory"))
        for gpu_resource, gpu_quantity in iter_gpu_resources(requests):
            gpu_map[gpu_resource] += gpu_quantity
    return cpu, memory_gib, dict(gpu_map)


def parse_profile_quotas(profile: Dict) -> Tuple[str, float, float, Dict[str, float]]:
    metadata = profile.get("metadata", {}) or {}
    spec = profile.get("spec", {}) or {}
    resource_quota_spec = spec.get("resourceQuotaSpec", {}) or {}
    hard = resource_quota_spec.get("hard", {}) or {}
    user_namespace = metadata.get("name", "")

    cpu_quota = parse_cpu_quantity(hard.get("requests.cpu"))
    memory_quota_gib = parse_memory_to_gib(hard.get("requests.memory"))
    gpu_quota: DefaultDict[str, float] = defaultdict(float)
    for resource_name, quantity in hard.items():
        if "gpu" not in str(resource_name).lower():
            continue
        try:
            gpu_quota[resource_name] += float(quantity)
        except ValueError:
            LOGGER.warning("Failed to parse profile GPU quota %r for %s.", quantity, resource_name)
    return user_namespace, cpu_quota, memory_quota_gib, dict(gpu_quota)


def collect_cluster_state(
    core_v1: client.CoreV1Api,
    custom_api: client.CustomObjectsApi,
) -> Tuple[list, list, list, list]:
    nodes = core_v1.list_node().items
    namespaces = core_v1.list_namespace().items
    pods = core_v1.list_pod_for_all_namespaces().items

    try:
        profile_list = custom_api.list_cluster_custom_object(
            group=PROFILE_GROUP,
            version=PROFILE_VERSION,
            plural=PROFILE_PLURAL,
        )
        profiles = profile_list.get("items", [])
    except ApiException as exc:
        LOGGER.warning("Failed to list profiles: %s", exc)
        profiles = []

    return nodes, namespaces, pods, profiles


def fetch_metric_samples_from_text(
    metrics_text: str,
    metric_names: Set[str],
) -> Dict[str, Dict[Tuple[Tuple[str, str], ...], float]]:
    parsed: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {
        name: {} for name in metric_names
    }
    for family in text_string_to_metric_families(metrics_text):
        for sample in family.samples:
            sample_name = sample[0]
            labels = sample[1]
            value = sample[2]
            if sample_name not in metric_names:
                continue
            parsed[sample_name][tuple(sorted(labels.items()))] = float(value)
    return parsed


def build_bootstrap_query(metric_name: str, service: str, lookback: str) -> str:
    # The counters only grow, so the highest value in the window is the last one
    # before the restart, and it survives Prometheus marking the old pod's series stale.
    # Prometheus stores the exporter's `namespace` label as `exported_namespace`.
    return (
        f'max by (exported_namespace) (max_over_time({metric_name}{{service="{service}"}}[{lookback}]))'
    )


def fetch_prometheus_bootstrap_samples(
    prometheus_url: str,
    service: str,
    lookback: str,
    timeout: float,
    evaluation_time: Optional[float] = None,
) -> Optional[Dict[str, Dict[Tuple[Tuple[str, str], ...], float]]]:
    """Return {metric: {(("namespace", ns),): value}}, or None if Prometheus could not answer.

    An empty result (a new cluster with no history yet) is a successful answer.
    """
    parsed: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
    try:
        for metric_name in LEGACY_COST_METRICS:
            params = {"query": build_bootstrap_query(metric_name, service, lookback)}
            if evaluation_time is not None:
                params["time"] = repr(float(evaluation_time))
            query = urllib.parse.urlencode(params)
            with urllib.request.urlopen(
                f"{prometheus_url}/api/v1/query?{query}", timeout=timeout
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") != "success":
                raise ValueError(f"Prometheus query failed: {payload.get('error')}")
            values: Dict[Tuple[Tuple[str, str], ...], float] = {}
            for item in payload.get("data", {}).get("result", []):
                namespace = item.get("metric", {}).get("exported_namespace")
                if namespace:
                    values[(("namespace", namespace),)] = float(item["value"][1])
            parsed[metric_name] = values
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as exc:
        LOGGER.warning("Failed to load cost bootstrap from Prometheus %s: %s", prometheus_url, exc)
        return None
    LOGGER.info(
        "Loaded cost bootstrap for %d namespaces from Prometheus %s (service=%s, lookback=%s).",
        len(parsed.get("namespace_cpu_cost", {})),
        prometheus_url,
        service,
        lookback,
    )
    return parsed


def fetch_legacy_metric_samples() -> Optional[Dict[str, Dict[Tuple[Tuple[str, str], ...], float]]]:
    """Cumulative cost samples to resume from; None means "source unreachable, retry later"."""
    if LEGACY_BOOTSTRAP_PROMETHEUS_URL:
        return fetch_prometheus_bootstrap_samples(
            LEGACY_BOOTSTRAP_PROMETHEUS_URL,
            LEGACY_BOOTSTRAP_SERVICE,
            LEGACY_BOOTSTRAP_LOOKBACK,
            LEGACY_BOOTSTRAP_TIMEOUT_SECONDS,
            PROCESS_START_TIME,
        )
    if not LEGACY_BOOTSTRAP_URL:
        return {}
    try:
        with urllib.request.urlopen(
            LEGACY_BOOTSTRAP_URL,
            timeout=LEGACY_BOOTSTRAP_TIMEOUT_SECONDS,
        ) as response:
            body = response.read().decode("utf-8")
        LOGGER.info("Loaded legacy bootstrap metrics from %s.", LEGACY_BOOTSTRAP_URL)
        return fetch_metric_samples_from_text(body, set(LEGACY_COST_METRICS))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        LOGGER.warning("Failed to load legacy bootstrap metrics from %s: %s", LEGACY_BOOTSTRAP_URL, exc)
        return None


def bootstrap_legacy_cost_state(
    legacy_cost_state: LegacyCostState,
    cpu_cost_per_minute: float,
    gpu_cost_per_minute: float,
) -> bool:
    if legacy_cost_state.bootstrapped:
        return True

    samples = fetch_legacy_metric_samples()
    if samples is None:
        return False

    # Until now the state holds only what accrued since this process started (from 0
    # while the source was unreachable), so add it on top of the restored totals.

    for labels, value in samples.get("namespace_cpu_cost", {}).items():
        namespace = dict(labels).get("namespace")
        if namespace:
            legacy_cost_state.cpu_cost[namespace] = value + legacy_cost_state.cpu_cost.get(namespace, 0.0)

    for labels, value in samples.get("namespace_gpu_cost", {}).items():
        namespace = dict(labels).get("namespace")
        if namespace:
            legacy_cost_state.gpu_cost[namespace] = value + legacy_cost_state.gpu_cost.get(namespace, 0.0)

    for labels, value in samples.get("namespace_cpu_cost_time", {}).items():
        namespace = dict(labels).get("namespace")
        if namespace:
            legacy_cost_state.cpu_cost_time[namespace] = value + legacy_cost_state.cpu_cost_time.get(namespace, 0.0)

    for labels, value in samples.get("namespace_gpu_cost_time", {}).items():
        namespace = dict(labels).get("namespace")
        if namespace:
            legacy_cost_state.gpu_cost_time[namespace] = value + legacy_cost_state.gpu_cost_time.get(namespace, 0.0)

    if cpu_cost_per_minute > 0:
        for namespace, value in legacy_cost_state.cpu_cost.items():
            legacy_cost_state.cpu_cost_time.setdefault(namespace, value / cpu_cost_per_minute)
    if gpu_cost_per_minute > 0:
        for namespace, value in legacy_cost_state.gpu_cost.items():
            legacy_cost_state.gpu_cost_time.setdefault(namespace, value / gpu_cost_per_minute)

    legacy_cost_state.bootstrapped = True
    legacy_cost_state.last_update_monotonic = None
    return True


def build_snapshot(
    nodes: list,
    namespaces: list,
    pods: list,
    profiles: list,
    cpu_cost_per_minute: float,
    gpu_cost_per_minute: float,
    legacy_cost_state: Optional[LegacyCostState] = None,
    now_monotonic: Optional[float] = None,
) -> Dict[str, Dict]:
    profile_namespaces = parse_profile_namespaces(profiles)

    semantic: Dict[str, Dict] = {
        "node_allocatable_cpu": {},
        "node_allocatable_memory": {},
        "node_allocatable_gpu": {},
        "node_billable_cpu": defaultdict(float),
        "node_billable_memory": defaultdict(float),
        "node_billable_gpu": defaultdict(float),
        "namespace_billable_cpu": defaultdict(float),
        "namespace_billable_memory": defaultdict(float),
        "namespace_billable_gpu": defaultdict(float),
        "namespace_workload_count": defaultdict(int),
        "namespace_notebook_count": defaultdict(int),
        "namespace_quota_cpu": {},
        "namespace_quota_memory": {},
        "namespace_quota_gpu": {},
        "namespace_billable_cpu_cost_rate": {},
        "namespace_billable_gpu_cost_rate": {},
        "namespace_billable_total_cost_rate": {},
        "workload_billable_cpu": defaultdict(float),
        "workload_billable_memory": defaultdict(float),
        "workload_billable_gpu": defaultdict(float),
        "notebook_billable_cpu": defaultdict(float),
        "notebook_billable_memory": defaultdict(float),
        "notebook_billable_gpu": defaultdict(float),
    }
    legacy: Dict[str, Dict] = {
        "node_cpu_allocatable": {},
        "node_gpu_allocatable": {},
        "node_memory_allocatable": {},
        "node_usage_cpu": {},
        "node_usage_gpu": {},
        "namespace_usage_cpu": {},
        "namespace_usage_gpu": {},
        "notebook_cpu_usage": {},
        "notebook_memory_usage": {},
        "notebook_gpu_usage": {},
        "namespace_cpu_cost": {},
        "namespace_cpu_cost_time": {},
        "namespace_gpu_cost": {},
        "namespace_gpu_cost_time": {},
    }

    for node in nodes:
        node_name = node.metadata.name
        allocatable = node.status.allocatable or {}
        cpu_allocatable = parse_cpu_quantity(allocatable.get("cpu"))
        memory_allocatable = parse_memory_to_gib(allocatable.get("memory"))
        semantic["node_allocatable_cpu"][node_name] = cpu_allocatable
        semantic["node_allocatable_memory"][node_name] = memory_allocatable
        legacy["node_cpu_allocatable"][node_name] = cpu_allocatable
        legacy["node_gpu_allocatable"][node_name] = float(allocatable.get("nvidia.com/gpu", 0) or 0)
        legacy["node_memory_allocatable"][node_name] = memory_allocatable
        legacy["node_usage_cpu"][node_name] = 0.0
        legacy["node_usage_gpu"][node_name] = 0.0
        for gpu_resource, quantity in iter_gpu_resources(allocatable):
            semantic["node_allocatable_gpu"][(node_name, gpu_resource)] = quantity

    for namespace in namespaces:
        namespace_name = namespace.metadata.name
        legacy["namespace_usage_cpu"][namespace_name] = 0.0
        legacy["namespace_usage_gpu"][namespace_name] = 0.0

    all_user_namespaces = set(profile_namespaces)
    for profile in profiles:
        user_namespace, cpu_quota, memory_quota_gib, gpu_quota = parse_profile_quotas(profile)
        if not user_namespace:
            continue
        semantic["namespace_quota_cpu"][user_namespace] = cpu_quota
        semantic["namespace_quota_memory"][user_namespace] = memory_quota_gib
        for gpu_resource, quantity in gpu_quota.items():
            semantic["namespace_quota_gpu"][(user_namespace, gpu_resource)] = quantity

    for pod in pods:
        node_name = pod.spec.node_name or ""
        namespace_name = pod.metadata.namespace or ""
        cpu_requested, memory_requested_gib, gpu_requested = collect_pod_requests(pod)

        legacy_notebook_name = legacy_identify_notebook_name(pod)
        if node_name and legacy_notebook_name:
            legacy["namespace_usage_cpu"][namespace_name] = (
                legacy["namespace_usage_cpu"].get(namespace_name, 0.0) + cpu_requested
            )
            legacy["namespace_usage_gpu"][namespace_name] = (
                legacy["namespace_usage_gpu"].get(namespace_name, 0.0) + sum(gpu_requested.values())
            )
            legacy["node_usage_cpu"][node_name] = legacy["node_usage_cpu"].get(node_name, 0.0) + cpu_requested
            legacy["node_usage_gpu"][node_name] = (
                legacy["node_usage_gpu"].get(node_name, 0.0) + sum(gpu_requested.values())
            )
            legacy_key = (node_name, namespace_name, legacy_notebook_name)
            legacy["notebook_cpu_usage"][legacy_key] = cpu_requested
            legacy["notebook_memory_usage"][legacy_key] = memory_requested_gib
            legacy["notebook_gpu_usage"][legacy_key] = sum(gpu_requested.values())

        if not should_track_pod(pod, profile_namespaces):
            continue

        user_namespace = namespace_name
        kubernetes_node = node_name
        workload_name, workload_kind, workload_type = identify_workload(pod)
        workload_key = (
            user_namespace,
            kubernetes_node,
            workload_name,
            workload_kind,
            workload_type,
        )

        all_user_namespaces.add(user_namespace)
        semantic["namespace_billable_cpu"][user_namespace] += cpu_requested
        semantic["namespace_billable_memory"][user_namespace] += memory_requested_gib
        semantic["namespace_workload_count"][user_namespace] += 1
        semantic["workload_billable_cpu"][workload_key] += cpu_requested
        semantic["workload_billable_memory"][workload_key] += memory_requested_gib

        if kubernetes_node:
            semantic["node_billable_cpu"][kubernetes_node] += cpu_requested
            semantic["node_billable_memory"][kubernetes_node] += memory_requested_gib

        notebook_name = identify_notebook_name(pod)
        if notebook_name and kubernetes_node:
            semantic["namespace_notebook_count"][user_namespace] += 1
            semantic["notebook_billable_cpu"][(user_namespace, kubernetes_node, notebook_name)] += (
                cpu_requested
            )
            semantic["notebook_billable_memory"][
                (user_namespace, kubernetes_node, notebook_name)
            ] += memory_requested_gib

        for gpu_resource, quantity in gpu_requested.items():
            semantic["namespace_billable_gpu"][(user_namespace, gpu_resource)] += quantity
            semantic["workload_billable_gpu"][workload_key + (gpu_resource,)] += quantity
            if kubernetes_node:
                semantic["node_billable_gpu"][(kubernetes_node, gpu_resource)] += quantity
                if notebook_name:
                    semantic["notebook_billable_gpu"][
                        (user_namespace, kubernetes_node, notebook_name, gpu_resource)
                    ] += quantity

    namespace_gpu_totals: DefaultDict[str, float] = defaultdict(float)
    for (user_namespace, _gpu_resource), value in semantic["namespace_billable_gpu"].items():
        namespace_gpu_totals[user_namespace] += value

    for user_namespace in all_user_namespaces:
        cpu_value = semantic["namespace_billable_cpu"].get(user_namespace, 0.0)
        gpu_value = namespace_gpu_totals.get(user_namespace, 0.0)
        semantic["namespace_billable_cpu_cost_rate"][user_namespace] = cpu_value * cpu_cost_per_minute
        semantic["namespace_billable_gpu_cost_rate"][user_namespace] = gpu_value * gpu_cost_per_minute
        semantic["namespace_billable_total_cost_rate"][user_namespace] = (
            semantic["namespace_billable_cpu_cost_rate"][user_namespace]
            + semantic["namespace_billable_gpu_cost_rate"][user_namespace]
        )

    if legacy_cost_state is not None:
        bootstrap_legacy_cost_state(legacy_cost_state, cpu_cost_per_minute, gpu_cost_per_minute)
        if cpu_cost_per_minute > 0:
            for namespace_name, value in legacy_cost_state.cpu_cost.items():
                legacy_cost_state.cpu_cost_time.setdefault(
                    namespace_name,
                    value / cpu_cost_per_minute,
                )
        if gpu_cost_per_minute > 0:
            for namespace_name, value in legacy_cost_state.gpu_cost.items():
                legacy_cost_state.gpu_cost_time.setdefault(
                    namespace_name,
                    value / gpu_cost_per_minute,
                )
        if now_monotonic is None:
            now_monotonic = time.monotonic()
        delta_minutes = 0.0
        if legacy_cost_state.last_update_monotonic is not None:
            delta_minutes = max(
                (now_monotonic - legacy_cost_state.last_update_monotonic) / 60.0,
                0.0,
            )
        legacy_cost_state.last_update_monotonic = now_monotonic

        for namespace_name, cpu_usage in legacy["namespace_usage_cpu"].items():
            if cpu_usage <= 0:
                continue
            legacy_cost_state.cpu_cost_time[namespace_name] = (
                legacy_cost_state.cpu_cost_time.get(namespace_name, 0.0)
                + (cpu_usage * delta_minutes)
            )
            legacy_cost_state.cpu_cost[namespace_name] = (
                legacy_cost_state.cpu_cost.get(namespace_name, 0.0)
                + (cpu_usage * cpu_cost_per_minute * delta_minutes)
            )

        for namespace_name, gpu_usage in legacy["namespace_usage_gpu"].items():
            if gpu_usage <= 0:
                continue
            legacy_cost_state.gpu_cost_time[namespace_name] = (
                legacy_cost_state.gpu_cost_time.get(namespace_name, 0.0)
                + (gpu_usage * delta_minutes)
            )
            legacy_cost_state.gpu_cost[namespace_name] = (
                legacy_cost_state.gpu_cost.get(namespace_name, 0.0)
                + (gpu_usage * gpu_cost_per_minute * delta_minutes)
            )

        tracked_namespaces = set(legacy["namespace_usage_cpu"].keys())
        tracked_namespaces.update(legacy_cost_state.cpu_cost.keys())
        tracked_namespaces.update(legacy_cost_state.gpu_cost.keys())
        tracked_namespaces.update(legacy_cost_state.cpu_cost_time.keys())
        tracked_namespaces.update(legacy_cost_state.gpu_cost_time.keys())

        for namespace_name in tracked_namespaces:
            legacy["namespace_cpu_cost"][namespace_name] = legacy_cost_state.cpu_cost.get(
                namespace_name,
                0.0,
            )
            legacy["namespace_cpu_cost_time"][namespace_name] = legacy_cost_state.cpu_cost_time.get(
                namespace_name,
                0.0,
            )
            legacy["namespace_gpu_cost"][namespace_name] = legacy_cost_state.gpu_cost.get(
                namespace_name,
                0.0,
            )
            legacy["namespace_gpu_cost_time"][namespace_name] = legacy_cost_state.gpu_cost_time.get(
                namespace_name,
                0.0,
            )

    return {
        "semantic": semantic,
        "legacy": legacy,
        "profile_namespaces": profile_namespaces,
    }


class MonitorMetrics:
    def __init__(self) -> None:
        self.scrape_success = Gauge(
            "cgu_monitor_scrape_success",
            "Whether the last monitor scrape and aggregation completed successfully.",
        )
        self.build_info = Gauge(
            "cgu_monitor_build_info",
            "Static build/version marker for the monitor exporter.",
            ["exporter_version"],
        )
        self.last_update_timestamp = Gauge(
            "cgu_monitor_last_update_timestamp_seconds",
            "Unix timestamp of the last successful metric refresh.",
        )
        self.legacy_bootstrap_success = Gauge(
            "cgu_monitor_legacy_bootstrap_success",
            "Whether legacy cost baseline was loaded successfully.",
        )
        self.cpu_cost_per_minute = Gauge(
            "cgu_cpu_cost_per_minute",
            "Configured CPU cost per minute used for billable usage calculations.",
        )
        self.gpu_cost_per_minute = Gauge(
            "cgu_gpu_cost_per_minute",
            "Configured GPU cost per minute used for billable usage calculations.",
        )
        self.node_allocatable_cpu = Gauge(
            "cgu_node_allocatable_cpu_cores",
            "Allocatable CPU cores per node.",
            ["kubernetes_node"],
        )
        self.node_allocatable_memory = Gauge(
            "cgu_node_allocatable_memory_gib",
            "Allocatable memory in GiB per node.",
            ["kubernetes_node"],
        )
        self.node_allocatable_gpu = Gauge(
            "cgu_node_allocatable_gpu_cards",
            "Allocatable GPU cards per node and GPU resource key.",
            ["kubernetes_node", "gpu_resource"],
        )
        self.node_billable_cpu = Gauge(
            "cgu_node_billable_cpu_cores",
            "Billable CPU usage by node based on pod requests.",
            ["kubernetes_node"],
        )
        self.node_billable_memory = Gauge(
            "cgu_node_billable_memory_gib",
            "Billable memory usage by node based on pod requests.",
            ["kubernetes_node"],
        )
        self.node_billable_gpu = Gauge(
            "cgu_node_billable_gpu_cards",
            "Billable GPU usage by node based on pod requests.",
            ["kubernetes_node", "gpu_resource"],
        )
        self.namespace_billable_cpu = Gauge(
            "cgu_namespace_billable_cpu_cores",
            "Billable CPU usage by user namespace based on pod requests.",
            ["user_namespace"],
        )
        self.namespace_billable_memory = Gauge(
            "cgu_namespace_billable_memory_gib",
            "Billable memory usage by user namespace based on pod requests.",
            ["user_namespace"],
        )
        self.namespace_billable_gpu = Gauge(
            "cgu_namespace_billable_gpu_cards",
            "Billable GPU usage by user namespace based on pod requests.",
            ["user_namespace", "gpu_resource"],
        )
        self.namespace_workload_count = Gauge(
            "cgu_namespace_workload_count",
            "Tracked user workload pod count by user namespace.",
            ["user_namespace"],
        )
        self.namespace_notebook_count = Gauge(
            "cgu_namespace_notebook_count",
            "Notebook pod count by user namespace.",
            ["user_namespace"],
        )
        self.namespace_quota_cpu = Gauge(
            "cgu_namespace_quota_cpu_cores",
            "CPU quota in cores from Kubeflow Profile resourceQuotaSpec.",
            ["user_namespace"],
        )
        self.namespace_quota_memory = Gauge(
            "cgu_namespace_quota_memory_gib",
            "Memory quota in GiB from Kubeflow Profile resourceQuotaSpec.",
            ["user_namespace"],
        )
        self.namespace_quota_gpu = Gauge(
            "cgu_namespace_quota_gpu_cards",
            "GPU quota from Kubeflow Profile resourceQuotaSpec.",
            ["user_namespace", "gpu_resource"],
        )
        self.namespace_billable_cpu_cost_rate = Gauge(
            "cgu_namespace_billable_cpu_cost_per_minute",
            "Instant CPU cost rate per minute by user namespace.",
            ["user_namespace"],
        )
        self.namespace_billable_gpu_cost_rate = Gauge(
            "cgu_namespace_billable_gpu_cost_per_minute",
            "Instant GPU cost rate per minute by user namespace.",
            ["user_namespace"],
        )
        self.namespace_billable_total_cost_rate = Gauge(
            "cgu_namespace_billable_total_cost_per_minute",
            "Instant total cost rate per minute by user namespace.",
            ["user_namespace"],
        )
        self.workload_billable_cpu = Gauge(
            "cgu_workload_billable_cpu_cores",
            "Billable CPU usage by tracked user workload based on pod requests.",
            ["user_namespace", "kubernetes_node", "workload_name", "workload_kind", "workload_type"],
        )
        self.workload_billable_memory = Gauge(
            "cgu_workload_billable_memory_gib",
            "Billable memory usage by tracked user workload based on pod requests.",
            ["user_namespace", "kubernetes_node", "workload_name", "workload_kind", "workload_type"],
        )
        self.workload_billable_gpu = Gauge(
            "cgu_workload_billable_gpu_cards",
            "Billable GPU usage by tracked user workload based on pod requests.",
            [
                "user_namespace",
                "kubernetes_node",
                "workload_name",
                "workload_kind",
                "workload_type",
                "gpu_resource",
            ],
        )
        self.notebook_billable_cpu = Gauge(
            "cgu_notebook_billable_cpu_cores",
            "Billable CPU usage by notebook based on pod requests.",
            ["user_namespace", "kubernetes_node", "notebook_name"],
        )
        self.notebook_billable_memory = Gauge(
            "cgu_notebook_billable_memory_gib",
            "Billable memory usage by notebook based on pod requests.",
            ["user_namespace", "kubernetes_node", "notebook_name"],
        )
        self.notebook_billable_gpu = Gauge(
            "cgu_notebook_billable_gpu_cards",
            "Billable GPU usage by notebook based on pod requests.",
            ["user_namespace", "kubernetes_node", "notebook_name", "gpu_resource"],
        )

        self.legacy_node_cpu_allocatable = Gauge(
            "node_cpu_allocatable",
            "Legacy-compatible allocatable CPU cores per node.",
            ["node"],
        )
        self.legacy_node_gpu_allocatable = Gauge(
            "node_gpu_allocatable",
            "Legacy-compatible allocatable GPU count per node.",
            ["node"],
        )
        self.legacy_node_memory_allocatable = Gauge(
            "node_memory_allocatable",
            "Legacy-compatible allocatable memory in GiB per node.",
            ["node"],
        )
        self.legacy_node_usage_cpu = Gauge(
            "node_usage_cpu",
            "Legacy-compatible node CPU usage based on pod requests.",
            ["node"],
        )
        self.legacy_node_usage_gpu = Gauge(
            "node_usage_gpu",
            "Legacy-compatible node GPU usage based on pod requests.",
            ["node"],
        )
        self.legacy_namespace_usage_cpu = Gauge(
            "namespace_usage_cpu",
            "Legacy-compatible namespace CPU usage based on pod requests.",
            ["namespace"],
        )
        self.legacy_namespace_usage_gpu = Gauge(
            "namespace_usage_gpu",
            "Legacy-compatible namespace GPU usage based on pod requests.",
            ["namespace"],
        )
        self.legacy_notebook_usage_cpu = Gauge(
            "notebook_cpu_usage",
            "Legacy-compatible notebook CPU usage based on pod requests.",
            ["node", "namespace", "notebook"],
        )
        self.legacy_notebook_usage_memory = Gauge(
            "notebook_memory_usage",
            "Legacy-compatible notebook memory usage based on pod requests.",
            ["node", "namespace", "notebook"],
        )
        self.legacy_notebook_usage_gpu = Gauge(
            "notebook_gpu_usage",
            "Legacy-compatible notebook GPU usage based on pod requests.",
            ["node", "namespace", "notebook"],
        )
        self.legacy_namespace_cpu_cost = Gauge(
            "namespace_cpu_cost",
            "Legacy-compatible cumulative CPU cost per namespace.",
            ["namespace"],
        )
        self.legacy_namespace_cpu_cost_time = Gauge(
            "namespace_cpu_cost_time",
            "Legacy-compatible cumulative CPU billable time per namespace in core-minutes.",
            ["namespace"],
        )
        self.legacy_namespace_gpu_cost = Gauge(
            "namespace_gpu_cost",
            "Legacy-compatible cumulative GPU cost per namespace.",
            ["namespace"],
        )
        self.legacy_namespace_gpu_cost_time = Gauge(
            "namespace_gpu_cost_time",
            "Legacy-compatible cumulative GPU billable time per namespace in gpu-minutes.",
            ["namespace"],
        )

        self._labeled_metrics = (
            self.node_allocatable_cpu,
            self.node_allocatable_memory,
            self.node_allocatable_gpu,
            self.node_billable_cpu,
            self.node_billable_memory,
            self.node_billable_gpu,
            self.namespace_billable_cpu,
            self.namespace_billable_memory,
            self.namespace_billable_gpu,
            self.namespace_workload_count,
            self.namespace_notebook_count,
            self.namespace_quota_cpu,
            self.namespace_quota_memory,
            self.namespace_quota_gpu,
            self.namespace_billable_cpu_cost_rate,
            self.namespace_billable_gpu_cost_rate,
            self.namespace_billable_total_cost_rate,
            self.workload_billable_cpu,
            self.workload_billable_memory,
            self.workload_billable_gpu,
            self.notebook_billable_cpu,
            self.notebook_billable_memory,
            self.notebook_billable_gpu,
            self.legacy_node_cpu_allocatable,
            self.legacy_node_gpu_allocatable,
            self.legacy_node_memory_allocatable,
            self.legacy_node_usage_cpu,
            self.legacy_node_usage_gpu,
            self.legacy_namespace_usage_cpu,
            self.legacy_namespace_usage_gpu,
            self.legacy_notebook_usage_cpu,
            self.legacy_notebook_usage_memory,
            self.legacy_notebook_usage_gpu,
            self.legacy_namespace_cpu_cost,
            self.legacy_namespace_cpu_cost_time,
            self.legacy_namespace_gpu_cost,
            self.legacy_namespace_gpu_cost_time,
        )
        self.build_info.labels(exporter_version=EXPORTER_VERSION).set(1)

    def clear_labeled_metrics(self) -> None:
        for metric in self._labeled_metrics:
            if hasattr(metric, "clear"):
                metric.clear()
                continue
            if hasattr(metric, "_lock") and hasattr(metric, "_metrics"):
                with metric._lock:
                    metric._metrics.clear()


def update_metrics(
    core_v1: client.CoreV1Api,
    custom_api: client.CustomObjectsApi,
    metrics: MonitorMetrics,
    legacy_cost_state: LegacyCostState,
) -> None:
    cpu_cost_per_minute, gpu_cost_per_minute = current_cost_settings()
    metrics.cpu_cost_per_minute.set(cpu_cost_per_minute)
    metrics.gpu_cost_per_minute.set(gpu_cost_per_minute)

    nodes, namespaces, pods, profiles = collect_cluster_state(core_v1, custom_api)
    snapshot = build_snapshot(
        nodes=nodes,
        namespaces=namespaces,
        pods=pods,
        profiles=profiles,
        cpu_cost_per_minute=cpu_cost_per_minute,
        gpu_cost_per_minute=gpu_cost_per_minute,
        legacy_cost_state=legacy_cost_state,
        now_monotonic=time.monotonic(),
    )
    metrics.legacy_bootstrap_success.set(1 if legacy_cost_state.bootstrapped else 0)
    metrics.clear_labeled_metrics()

    semantic = snapshot["semantic"]
    legacy = snapshot["legacy"]

    for node_name, value in semantic["node_allocatable_cpu"].items():
        metrics.node_allocatable_cpu.labels(kubernetes_node=node_name).set(value)
    for node_name, value in semantic["node_allocatable_memory"].items():
        metrics.node_allocatable_memory.labels(kubernetes_node=node_name).set(value)
    for (node_name, gpu_resource), value in semantic["node_allocatable_gpu"].items():
        metrics.node_allocatable_gpu.labels(
            kubernetes_node=node_name,
            gpu_resource=gpu_resource,
        ).set(value)
    for node_name, value in semantic["node_billable_cpu"].items():
        metrics.node_billable_cpu.labels(kubernetes_node=node_name).set(value)
    for node_name, value in semantic["node_billable_memory"].items():
        metrics.node_billable_memory.labels(kubernetes_node=node_name).set(value)
    for (node_name, gpu_resource), value in semantic["node_billable_gpu"].items():
        metrics.node_billable_gpu.labels(
            kubernetes_node=node_name,
            gpu_resource=gpu_resource,
        ).set(value)
    for user_namespace, value in semantic["namespace_billable_cpu"].items():
        metrics.namespace_billable_cpu.labels(user_namespace=user_namespace).set(value)
    for user_namespace, value in semantic["namespace_billable_memory"].items():
        metrics.namespace_billable_memory.labels(user_namespace=user_namespace).set(value)
    for (user_namespace, gpu_resource), value in semantic["namespace_billable_gpu"].items():
        metrics.namespace_billable_gpu.labels(
            user_namespace=user_namespace,
            gpu_resource=gpu_resource,
        ).set(value)
    all_user_namespaces = set(snapshot["profile_namespaces"])
    all_user_namespaces.update(semantic["namespace_billable_cpu"].keys())
    all_user_namespaces.update(semantic["namespace_workload_count"].keys())
    all_user_namespaces.update(semantic["namespace_notebook_count"].keys())
    all_user_namespaces.update(semantic["namespace_quota_cpu"].keys())
    for user_namespace in all_user_namespaces:
        metrics.namespace_workload_count.labels(user_namespace=user_namespace).set(
            semantic["namespace_workload_count"].get(user_namespace, 0)
        )
        metrics.namespace_notebook_count.labels(user_namespace=user_namespace).set(
            semantic["namespace_notebook_count"].get(user_namespace, 0)
        )
        metrics.namespace_billable_cpu_cost_rate.labels(user_namespace=user_namespace).set(
            semantic["namespace_billable_cpu_cost_rate"].get(user_namespace, 0.0)
        )
        metrics.namespace_billable_gpu_cost_rate.labels(user_namespace=user_namespace).set(
            semantic["namespace_billable_gpu_cost_rate"].get(user_namespace, 0.0)
        )
        metrics.namespace_billable_total_cost_rate.labels(user_namespace=user_namespace).set(
            semantic["namespace_billable_total_cost_rate"].get(user_namespace, 0.0)
        )
        metrics.namespace_quota_cpu.labels(user_namespace=user_namespace).set(
            semantic["namespace_quota_cpu"].get(user_namespace, 0.0)
        )
        metrics.namespace_quota_memory.labels(user_namespace=user_namespace).set(
            semantic["namespace_quota_memory"].get(user_namespace, 0.0)
        )
    for (user_namespace, gpu_resource), value in semantic["namespace_quota_gpu"].items():
        metrics.namespace_quota_gpu.labels(
            user_namespace=user_namespace,
            gpu_resource=gpu_resource,
        ).set(value)
    for labels, value in semantic["workload_billable_cpu"].items():
        user_namespace, kubernetes_node, workload_name, workload_kind, workload_type = labels
        metrics.workload_billable_cpu.labels(
            user_namespace=user_namespace,
            kubernetes_node=kubernetes_node,
            workload_name=workload_name,
            workload_kind=workload_kind,
            workload_type=workload_type,
        ).set(value)
    for labels, value in semantic["workload_billable_memory"].items():
        user_namespace, kubernetes_node, workload_name, workload_kind, workload_type = labels
        metrics.workload_billable_memory.labels(
            user_namespace=user_namespace,
            kubernetes_node=kubernetes_node,
            workload_name=workload_name,
            workload_kind=workload_kind,
            workload_type=workload_type,
        ).set(value)
    for labels, value in semantic["workload_billable_gpu"].items():
        (
            user_namespace,
            kubernetes_node,
            workload_name,
            workload_kind,
            workload_type,
            gpu_resource,
        ) = labels
        metrics.workload_billable_gpu.labels(
            user_namespace=user_namespace,
            kubernetes_node=kubernetes_node,
            workload_name=workload_name,
            workload_kind=workload_kind,
            workload_type=workload_type,
            gpu_resource=gpu_resource,
        ).set(value)
    for labels, value in semantic["notebook_billable_cpu"].items():
        user_namespace, kubernetes_node, notebook_name = labels
        metrics.notebook_billable_cpu.labels(
            user_namespace=user_namespace,
            kubernetes_node=kubernetes_node,
            notebook_name=notebook_name,
        ).set(value)
    for labels, value in semantic["notebook_billable_memory"].items():
        user_namespace, kubernetes_node, notebook_name = labels
        metrics.notebook_billable_memory.labels(
            user_namespace=user_namespace,
            kubernetes_node=kubernetes_node,
            notebook_name=notebook_name,
        ).set(value)
    for labels, value in semantic["notebook_billable_gpu"].items():
        user_namespace, kubernetes_node, notebook_name, gpu_resource = labels
        metrics.notebook_billable_gpu.labels(
            user_namespace=user_namespace,
            kubernetes_node=kubernetes_node,
            notebook_name=notebook_name,
            gpu_resource=gpu_resource,
        ).set(value)

    for node_name, value in legacy["node_cpu_allocatable"].items():
        metrics.legacy_node_cpu_allocatable.labels(node=node_name).set(value)
    for node_name, value in legacy["node_gpu_allocatable"].items():
        metrics.legacy_node_gpu_allocatable.labels(node=node_name).set(value)
    for node_name, value in legacy["node_memory_allocatable"].items():
        metrics.legacy_node_memory_allocatable.labels(node=node_name).set(value)
    for node_name, value in legacy["node_usage_cpu"].items():
        metrics.legacy_node_usage_cpu.labels(node=node_name).set(value)
    for node_name, value in legacy["node_usage_gpu"].items():
        metrics.legacy_node_usage_gpu.labels(node=node_name).set(value)
    for namespace_name, value in legacy["namespace_usage_cpu"].items():
        metrics.legacy_namespace_usage_cpu.labels(namespace=namespace_name).set(value)
    for namespace_name, value in legacy["namespace_usage_gpu"].items():
        metrics.legacy_namespace_usage_gpu.labels(namespace=namespace_name).set(value)
    for labels, value in legacy["notebook_cpu_usage"].items():
        node_name, namespace_name, notebook_name = labels
        metrics.legacy_notebook_usage_cpu.labels(
            node=node_name,
            namespace=namespace_name,
            notebook=notebook_name,
        ).set(value)
    for labels, value in legacy["notebook_memory_usage"].items():
        node_name, namespace_name, notebook_name = labels
        metrics.legacy_notebook_usage_memory.labels(
            node=node_name,
            namespace=namespace_name,
            notebook=notebook_name,
        ).set(value)
    for labels, value in legacy["notebook_gpu_usage"].items():
        node_name, namespace_name, notebook_name = labels
        metrics.legacy_notebook_usage_gpu.labels(
            node=node_name,
            namespace=namespace_name,
            notebook=notebook_name,
        ).set(value)
    for namespace_name, value in legacy["namespace_cpu_cost"].items():
        metrics.legacy_namespace_cpu_cost.labels(namespace=namespace_name).set(value)
    for namespace_name, value in legacy["namespace_cpu_cost_time"].items():
        metrics.legacy_namespace_cpu_cost_time.labels(namespace=namespace_name).set(value)
    for namespace_name, value in legacy["namespace_gpu_cost"].items():
        metrics.legacy_namespace_gpu_cost.labels(namespace=namespace_name).set(value)
    for namespace_name, value in legacy["namespace_gpu_cost_time"].items():
        metrics.legacy_namespace_gpu_cost_time.labels(namespace=namespace_name).set(value)

    metrics.last_update_timestamp.set(time.time())
    LOGGER.info(
        "Updated snapshot: %s profiles, %s semantic workloads, %s legacy tracked pods.",
        len(snapshot["profile_namespaces"]),
        len(semantic["workload_billable_cpu"]),
        len(legacy["notebook_cpu_usage"]),
    )


def main() -> None:
    load_kubernetes_config()
    core_v1 = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()
    metrics = MonitorMetrics()
    legacy_cost_state = LegacyCostState()

    start_http_server(SCRAPE_PORT)
    LOGGER.info("Started Prometheus exporter on port %s with version %s.", SCRAPE_PORT, EXPORTER_VERSION)

    while True:
        try:
            update_metrics(core_v1, custom_api, metrics, legacy_cost_state)
            metrics.scrape_success.set(1)
        except Exception as exc:
            LOGGER.exception("Failed to update metrics: %s", exc)
            metrics.scrape_success.set(0)
        time.sleep(UPDATE_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
