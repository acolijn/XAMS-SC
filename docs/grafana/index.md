# Grafana

Grafana reads PostgreSQL and nothing else. It is a view, never a source: a
dashboard can be deleted and rebuilt without touching any data.

Local instance on port 3000; the **XAMS Overview** dashboard is the one to
open.

!!! warning "Not written yet"
    Wanted: install and provisioning on the lab PC, the datasource definition, and how the dashboards under `grafana/provisioning/` are loaded.
