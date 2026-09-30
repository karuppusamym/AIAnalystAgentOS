# BI destinations and workspace scope

AnalystOS currently publishes to Apache Superset or its in-platform preview. A `powerbi`
value remains in the portable publish-bundle contract for a future adapter, but there is no
Power BI publisher, authentication configuration, or Power BI workspace mapping. Setting a
workspace policy's `publish_destinations` to include `powerbi` does not enable it. An explicit
Power BI request is rejected before approval; an automatic destination choice skips it.

For Superset, enable the `bi` Compose profile and configure the Superset connection described
in the deployment runbook. Each AnalystOS workspace keeps its own publication records and
approval policy. Superset datasets, charts, and dashboards carry a workspace-scoped name,
slug, or provenance marker. The publisher also creates a workspace role. This is a mapping
of AnalystOS workspaces within Superset, not a Power BI workspace category.

Power BI publication would require a separate adapter that authenticates to Microsoft,
maps each AnalystOS workspace to an authorized Power BI workspace, reconciles datasets and
reports on retry, preserves the approved content hash, and records external IDs for rollback.
Until that exists, configure Superset for external BI publication or use the in-platform
preview.

For a future Power BI adapter, Microsoft describes an Entra application/service principal,
the tenant setting allowing service principals to use Power BI APIs, and access for that
principal to the destination workspace. See [service principal setup](https://learn.microsoft.com/en-us/power-bi/developer/embedded/embed-service-principal).
Power BI workspaces are content containers with Admin, Member, Contributor, and Viewer roles;
publishing and viewing can have license or capacity requirements. See [workspace roles](https://learn.microsoft.com/en-us/power-bi/collaborate-share/service-roles-new-workspaces).
Workspace folders can group content *inside* a Power BI workspace, and Fabric domains can
logically group workspaces without granting access. See [Microsoft workspace planning](https://learn.microsoft.com/en-us/power-bi/guidance/powerbi-implementation-planning-workspaces-workspace-level-planning)
and [content distribution guidance](https://learn.microsoft.com/en-us/power-bi/guidance/powerbi-implementation-planning-content-distribution-sharing).
AnalystOS does not currently create either mapping or categorization in Power BI.
