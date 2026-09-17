# Drift test

A dashboard that plots a channel which no longer exists is worse than no
dashboard. `tests/test_grafana_drift.py` compares the saved dashboards against
`channels.yaml` and fails when they disagree.

!!! warning "Not written yet"
    Wanted: what the test checks, what a failure means, and how to fix one.
