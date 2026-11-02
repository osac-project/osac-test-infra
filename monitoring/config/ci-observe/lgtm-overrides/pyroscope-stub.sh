#!/usr/bin/env bash
# ci-observe: stub for the grafana/otel-lgtm Pyroscope component.
#
# Pyroscope cannot start under rootless podman's pasta networking: its ring
# lifecyclers (overrides-exporter, segment-writer, ...) insist on a usable
# address on eth0 or en0, and pasta mirrors the host's interface names (on
# the central CI machine there is no eth0 at all, only the host's bond1), so
# every module fails with "no useable address found" and the image's
# entrypoint aborts the whole stack.
#
# The capture stack uses traces, metrics and logs only; profiles were never
# part of the pipeline. This stub keeps the component slot alive (the
# entrypoint fails the container only when a component process exits); it
# keeps waiting for Pyroscope's readiness while Grafana, Loki, Tempo,
# Prometheus and the OTLP collector serve normally.
while true; do
    sleep 3600
done
