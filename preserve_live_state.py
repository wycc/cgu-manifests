#!/usr/bin/env python3
"""Make re-running apply.sh on a running cluster safe.

Reads the kustomize output on stdin and writes the manifests to apply on stdout. Objects that
do not exist yet pass through unchanged, so a new cluster is installed exactly as before. For
objects that already exist, state that is owned by the cluster rather than by this repository
is kept:

- PersistentVolumeClaims and the ConfigMaps in CREATE_ONLY are created once and never
  re-applied: a PVC spec cannot change after creation (the apply fails and apply.sh used to
  retry forever), and those ConfigMaps hold data edited on the live cluster.
- caBundle of webhooks and CRD conversion webhooks is injected by cert-manager; the
  manifests carry an empty one, and applying it would reject every request to that webhook
  until it is injected again.
- rules of aggregated ClusterRoles are filled in by the aggregation controller; knative
  rewrites the rules of its webhooks (the manifests hold placeholders) and istiod sets the
  failurePolicy of istio-validator once it is ready.

Namespaces listed in APPLY_SKIP_NAMESPACES (comma separated, from env.ini) are left out
entirely, for components a cluster deliberately does not run.
"""
import json
import os
import subprocess
import sys

import yaml

CREATE_ONLY = {
    ("ConfigMap", "kubeflow", "kubeflow-announcements"),
    ("ConfigMap", "kubeflow", "qnap-config"),
}
WEBHOOK_KINDS = {"MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"}
RULES_OWNED_BY_CONTROLLER = (".knative.dev",)  # webhook configuration name suffixes
FAILURE_POLICY_OWNED_BY_CONTROLLER = ("istio-validator",)  # webhook configuration name prefixes
SKIP_NAMESPACES = {n.strip() for n in os.environ.get("APPLY_SKIP_NAMESPACES", "").split(",") if n.strip()}


def log(message):
    print(f"[preserve-live-state] {message}", file=sys.stderr)


def get_live(kind, namespace, name):
    command = ["kubectl", "get", kind, name, "-o", "json"]
    if namespace:
        command += ["-n", namespace]
    result = subprocess.run(command, capture_output=True, text=True)
    return json.loads(result.stdout) if result.returncode == 0 else None


def keep_webhook_state(doc, live):
    name = (doc.get("metadata") or {}).get("name", "")
    rules_live = name.endswith(RULES_OWNED_BY_CONTROLLER)
    policy_live = name.startswith(FAILURE_POLICY_OWNED_BY_CONTROLLER)
    live_hooks = {hook.get("name"): hook for hook in live.get("webhooks") or []}
    for hook in doc.get("webhooks") or []:
        current = live_hooks.get(hook.get("name"))
        if not current:
            continue
        client = hook.get("clientConfig") or {}
        if "caBundle" in client and "caBundle" in (current.get("clientConfig") or {}):
            client["caBundle"] = current["clientConfig"]["caBundle"]
        if "rules" in current and (rules_live or "rules" not in hook):
            hook["rules"] = current["rules"]
        if policy_live and "failurePolicy" in current:
            hook["failurePolicy"] = current["failurePolicy"]


def keep_crd_ca_bundle(doc, live):
    client = (((doc.get("spec") or {}).get("conversion") or {}).get("webhook") or {}).get("clientConfig")
    live_client = (((live.get("spec") or {}).get("conversion") or {}).get("webhook") or {}).get("clientConfig") or {}
    if client is not None and "caBundle" in client and "caBundle" in live_client:
        client["caBundle"] = live_client["caBundle"]


def main():
    out = []
    for doc in yaml.safe_load_all(sys.stdin):
        if not doc:
            continue
        kind = doc.get("kind", "")
        metadata = doc.get("metadata") or {}
        namespace, name = metadata.get("namespace"), metadata.get("name")
        if namespace in SKIP_NAMESPACES or (kind == "Namespace" and name in SKIP_NAMESPACES):
            log(f"skip {kind} {namespace or ''}/{name} (APPLY_SKIP_NAMESPACES)")
            continue

        create_only = kind == "PersistentVolumeClaim" or (kind, namespace, name) in CREATE_ONLY
        needs_live = create_only or kind in WEBHOOK_KINDS or kind == "CustomResourceDefinition" or (
            kind == "ClusterRole" and doc.get("aggregationRule"))
        live = get_live(kind, namespace, name) if needs_live else None

        if live is not None:
            if create_only:
                log(f"keep existing {kind} {namespace}/{name}")
                continue
            if kind in WEBHOOK_KINDS:
                keep_webhook_state(doc, live)
            elif kind == "CustomResourceDefinition":
                keep_crd_ca_bundle(doc, live)
            elif kind == "ClusterRole" and "rules" in live:
                doc["rules"] = live["rules"]
        out.append(doc)
    yaml.safe_dump_all(out, sys.stdout, sort_keys=False)


if __name__ == "__main__":
    main()
