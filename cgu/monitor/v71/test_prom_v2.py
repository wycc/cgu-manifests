import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import prom_v2 as prom


class ParseQuantityTests(unittest.TestCase):
    def test_parse_cpu_quantity(self):
        self.assertEqual(prom.parse_cpu_quantity("500m"), 0.5)
        self.assertEqual(prom.parse_cpu_quantity("2"), 2.0)

    def test_parse_memory_to_gib_binary(self):
        self.assertAlmostEqual(prom.parse_memory_to_gib("1024Mi"), 1.0)
        self.assertAlmostEqual(prom.parse_memory_to_gib("1Gi"), 1.0)

    def test_parse_memory_to_gib_decimal(self):
        self.assertAlmostEqual(prom.parse_memory_to_gib("1G"), 1.0)
        self.assertAlmostEqual(prom.parse_memory_to_gib("2048M"), 2.0)

    def test_parse_every_valid_kubernetes_quantity(self):
        self.assertAlmostEqual(prom.parse_memory_to_gib("1k"), 1 / (1024.0 * 1024.0))
        self.assertAlmostEqual(prom.parse_memory_to_gib("400m"), 0.4 / 1024.0**3)
        self.assertAlmostEqual(prom.parse_memory_to_gib("1e3"), 1000 / 1024.0**3)
        self.assertAlmostEqual(prom.parse_memory_to_gib("1.5Gi"), 1.5)
        self.assertAlmostEqual(prom.parse_memory_to_gib("1Ei"), 1024.0**3)
        self.assertAlmostEqual(prom.parse_memory_to_gib("1073741824"), 1.0)
        self.assertAlmostEqual(prom.parse_cpu_quantity("2k"), 2000.0)
        self.assertAlmostEqual(prom.parse_cpu_quantity("250000u"), 0.25)
        self.assertAlmostEqual(prom.parse_cpu_quantity("1e-1"), 0.1)

    def test_unparsable_quantity_counts_as_zero_instead_of_raising(self):
        with self.assertLogs(prom.LOGGER, level="WARNING"):
            self.assertEqual(prom.parse_memory_to_gib("lots"), 0.0)
            self.assertEqual(prom.parse_cpu_quantity("1 core"), 0.0)

    def test_one_odd_pod_request_does_not_abort_the_pod(self):
        container = mock.Mock()
        container.resources.requests = {"cpu": "1k", "memory": "400m", "nvidia.com/gpu": "1"}
        pod = mock.Mock()
        pod.spec.containers = [container]
        cpu, memory_gib, gpus = prom.collect_pod_requests(pod)
        self.assertEqual(cpu, 1000.0)
        self.assertAlmostEqual(memory_gib, 0.4 / 1024.0**3)
        self.assertEqual(gpus, {"nvidia.com/gpu": 1.0})

    def test_iter_gpu_resources(self):
        resources = dict(prom.iter_gpu_resources({"nvidia.com/gpu": "2", "cpu": "1"}))
        self.assertEqual(resources, {"nvidia.com/gpu": 2.0})


class DummyOwner:
    def __init__(self, kind, name, controller=True):
        self.kind = kind
        self.name = name
        self.controller = controller


class DummyMeta:
    def __init__(self, name, namespace="demo", labels=None, owner_references=None):
        self.name = name
        self.namespace = namespace
        self.labels = labels or {}
        self.owner_references = owner_references or []


class DummyStatus:
    def __init__(self, phase="Running"):
        self.phase = phase


class DummyPod:
    def __init__(self, name, namespace="demo", labels=None, owner_references=None, phase="Running"):
        self.metadata = DummyMeta(name, namespace, labels, owner_references)
        self.status = DummyStatus(phase)


class NotebookNameTests(unittest.TestCase):
    def test_identify_notebook_from_label(self):
        pod = DummyPod("x", labels={"notebook-name": "demo"})
        self.assertEqual(prom.identify_notebook_name(pod), "demo")

    def test_identify_notebook_requires_explicit_label(self):
        pod = DummyPod(
            "demo-0",
            owner_references=[DummyOwner("StatefulSet", "demo")],
        )
        self.assertIsNone(prom.identify_notebook_name(pod))

    def test_legacy_identify_notebook_name_uses_suffix(self):
        pod = DummyPod("openldap-0")
        self.assertEqual(prom.legacy_identify_notebook_name(pod), "openldap-0")

    def test_legacy_identify_notebook_name_rejects_non_suffix_name(self):
        pod = DummyPod("backend-abc")
        self.assertIsNone(prom.legacy_identify_notebook_name(pod))


class WorkloadIdentificationTests(unittest.TestCase):
    def test_normalize_replicaset_name(self):
        self.assertEqual(
            prom.normalize_replicaset_name("pvcviewer-a1-workspace-64ccf9ff6c"),
            "pvcviewer-a1-workspace",
        )

    def test_identify_workload_for_notebook(self):
        pod = DummyPod("notebook1-0", labels={"notebook-name": "notebook1"})
        self.assertEqual(
            prom.identify_workload(pod),
            ("notebook1", "StatefulSet", "notebook"),
        )

    def test_identify_workload_for_pvcviewer(self):
        pod = DummyPod(
            "pvcviewer-a1-workspace-64ccf9ff6c-78x56",
            labels={
                "app.kubernetes.io/instance": "pvcviewer-a1-workspace",
                "app.kubernetes.io/part-of": "pvc-viewer",
            },
            owner_references=[DummyOwner("ReplicaSet", "pvcviewer-a1-workspace-64ccf9ff6c")],
        )
        self.assertEqual(
            prom.identify_workload(pod),
            ("pvcviewer-a1-workspace", "Deployment", "pvcviewer"),
        )

    def test_identify_workload_for_deployment(self):
        pod = DummyPod(
            "backend-deployment-6ff76cd8cd-9nr2l",
            labels={"app": "backend"},
            owner_references=[DummyOwner("ReplicaSet", "backend-deployment-6ff76cd8cd")],
        )
        self.assertEqual(
            prom.identify_workload(pod),
            ("backend-deployment", "Deployment", "deployment"),
        )


class ProfileNamespaceTests(unittest.TestCase):
    def test_parse_profile_namespaces(self):
        profiles = [
            {"metadata": {"name": "b1144209"}},
            {"metadata": {"name": "wycctest1"}},
            {"metadata": {}},
        ]
        self.assertEqual(
            prom.parse_profile_namespaces(profiles),
            {"b1144209", "wycctest1"},
        )

    def test_should_track_only_profile_namespaces(self):
        profile_namespaces = {"b1144209", "wycctest1"}
        tracked_pod = DummyPod("notebook1-0", namespace="b1144209")
        system_pod = DummyPod("prometheus-0", namespace="prometheus")
        finished_pod = DummyPod("old-0", namespace="wycctest1", phase="Succeeded")

        self.assertTrue(prom.should_track_pod(tracked_pod, profile_namespaces))
        self.assertFalse(prom.should_track_pod(system_pod, profile_namespaces))
        self.assertFalse(prom.should_track_pod(finished_pod, profile_namespaces))


class LegacyBootstrapTests(unittest.TestCase):
    def test_bootstrap_derives_cost_time_when_missing(self):
        state = prom.LegacyCostState(
            cpu_cost={"b1144209": 120.0},
            gpu_cost={"b1128005": 50.0},
        )
        state.bootstrapped = True
        prom.build_snapshot([], [], [], [], 2.0, 10.0, state, now_monotonic=100.0)
        self.assertEqual(state.cpu_cost_time["b1144209"], 60.0)
        self.assertEqual(state.gpu_cost_time["b1128005"], 5.0)


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def prometheus_answer(values_by_metric):
    """A fake urlopen that answers each bootstrap query from values_by_metric."""
    seen = []

    def urlopen(url, timeout):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
        seen.append((url, query, timeout))
        metric = next(m for m in prom.LEGACY_COST_METRICS if f"({m}{{" in query)
        result = [
            {"metric": {"exported_namespace": ns}, "value": [1795105827.1, str(v)]}
            for ns, v in values_by_metric.get(metric, {}).items()
        ]
        return FakeResponse({"status": "success", "data": {"resultType": "vector", "result": result}})

    return urlopen, seen


class PrometheusBootstrapTests(unittest.TestCase):
    def test_query_reads_back_own_series_high_water_mark(self):
        self.assertEqual(
            prom.build_bootstrap_query("namespace_gpu_cost", "prom-app-v71", "30d"),
            'max by (exported_namespace) (max_over_time(namespace_gpu_cost{service="prom-app-v71"}[30d]))',
        )

    def test_samples_are_keyed_by_exported_namespace(self):
        urlopen, seen = prometheus_answer(
            {"namespace_cpu_cost": {"b1144209": 12.5}, "namespace_gpu_cost_time": {"d1300701": 300.0}}
        )
        with mock.patch.object(prom.urllib.request, "urlopen", urlopen):
            samples = prom.fetch_prometheus_bootstrap_samples("http://prom:9090", "prom-app-v71", "30d", 7.0)

        self.assertEqual(samples["namespace_cpu_cost"], {(("namespace", "b1144209"),): 12.5})
        self.assertEqual(samples["namespace_gpu_cost_time"], {(("namespace", "d1300701"),): 300.0})
        self.assertEqual(samples["namespace_gpu_cost"], {})
        self.assertEqual(len(seen), 4)
        self.assertTrue(all(url.startswith("http://prom:9090/api/v1/query?") for url, _, _ in seen))
        self.assertTrue(all(timeout == 7.0 for _, _, timeout in seen))

    def test_bootstrap_reads_prometheus_as_of_process_start(self):
        # Samples scraped from this process after it started must not become the baseline,
        # or a late bootstrap would add its own accrual twice.
        urlopen, seen = prometheus_answer({})
        with mock.patch.object(prom.urllib.request, "urlopen", urlopen):
            prom.fetch_prometheus_bootstrap_samples("http://prom:9090", "prom-app-v71", "30d", 5.0, 1795105800.25)
        times = {urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["time"][0] for url, _, _ in seen}
        self.assertEqual(times, {"1795105800.25"})

    def test_late_bootstrap_uses_the_process_start_time(self):
        urlopen, seen = prometheus_answer({})
        with mock.patch.object(prom, "LEGACY_BOOTSTRAP_PROMETHEUS_URL", "http://prom:9090"), mock.patch.object(
            prom, "PROCESS_START_TIME", 1795105000.0
        ), mock.patch.object(prom.urllib.request, "urlopen", urlopen):
            self.assertTrue(prom.bootstrap_legacy_cost_state(prom.LegacyCostState(), 0.03, 0.7))
        times = {urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["time"][0] for url, _, _ in seen}
        self.assertEqual(times, {"1795105000.0"})

    def test_unreachable_prometheus_is_retried_not_treated_as_empty(self):
        def unreachable(url, timeout):
            raise urllib.error.URLError("connection refused")

        state = prom.LegacyCostState()
        with mock.patch.object(prom, "LEGACY_BOOTSTRAP_PROMETHEUS_URL", "http://prom:9090"), mock.patch.object(
            prom.urllib.request, "urlopen", unreachable
        ):
            self.assertIsNone(prom.fetch_legacy_metric_samples())
            self.assertFalse(prom.bootstrap_legacy_cost_state(state, 0.03, 0.7))
        self.assertFalse(state.bootstrapped)

    def test_failed_query_status_is_retried(self):
        def bad_query(url, timeout):
            return FakeResponse({"status": "error", "error": "parse error"})

        with mock.patch.object(prom.urllib.request, "urlopen", bad_query):
            self.assertIsNone(
                prom.fetch_prometheus_bootstrap_samples("http://prom:9090", "prom-app-v71", "30d", 5.0)
            )

    def test_new_cluster_without_history_bootstraps_from_zero(self):
        urlopen, _ = prometheus_answer({})
        state = prom.LegacyCostState()
        with mock.patch.object(prom, "LEGACY_BOOTSTRAP_PROMETHEUS_URL", "http://prom:9090"), mock.patch.object(
            prom.urllib.request, "urlopen", urlopen
        ):
            self.assertTrue(prom.bootstrap_legacy_cost_state(state, 0.03, 0.7))
        self.assertTrue(state.bootstrapped)
        self.assertEqual(state.cpu_cost, {})

    def test_cost_accrued_before_a_late_bootstrap_is_kept(self):
        # Prometheus was down at startup, so 2.0 accrued from zero; the restored total adds on top.
        urlopen, _ = prometheus_answer(
            {
                "namespace_cpu_cost": {"b1144209": 100.0},
                "namespace_cpu_cost_time": {"b1144209": 3000.0},
            }
        )
        state = prom.LegacyCostState(cpu_cost={"b1144209": 2.0}, cpu_cost_time={"b1144209": 60.0})
        with mock.patch.object(prom, "LEGACY_BOOTSTRAP_PROMETHEUS_URL", "http://prom:9090"), mock.patch.object(
            prom.urllib.request, "urlopen", urlopen
        ):
            self.assertTrue(prom.bootstrap_legacy_cost_state(state, 0.03, 0.7))
        self.assertEqual(state.cpu_cost["b1144209"], 102.0)
        self.assertEqual(state.cpu_cost_time["b1144209"], 3060.0)

    def test_no_source_configured_starts_from_zero(self):
        state = prom.LegacyCostState()
        with mock.patch.object(prom, "LEGACY_BOOTSTRAP_PROMETHEUS_URL", ""), mock.patch.object(
            prom, "LEGACY_BOOTSTRAP_URL", ""
        ):
            self.assertTrue(prom.bootstrap_legacy_cost_state(state, 0.03, 0.7))
        self.assertTrue(state.bootstrapped)


if __name__ == "__main__":
    unittest.main()
