# Context2AI AnalystOS
## Autonomous Data & Analytics Agent Operating System

**Document type:** Product + Architecture + Engineering Execution Specification  
**Status:** Draft for implementation  
**Primary goal:** Build an enterprise-grade, fully agentic analytics platform that can autonomously perform a large portion of the work done by data analysts, analytics engineers, BI developers, data engineers, and data scientists while keeping humans in control.

> **Specification boundary:** This is the target product and engineering specification, not a statement that every capability is already implemented. Treat tracker defaults as planning placeholders until each item has code, test, and deployment evidence. The AIDataAnalyst and AIDataEngineerAgentOS reviews cited in §70 describe separate implementations: their status and capabilities must not be reported as AnalystOS completion evidence.


use OpenRouter with OPENROUTER_API_KEY supplied through the environment; never commit the key.

---

# 1. Executive Summary

Context2AI AnalystOS is an autonomous data and analytics operating system built on top of the existing Context2AI platform.

The existing Context2AI application already provides a strong foundation:

- Source metadata collection
- AI-assisted and user-assisted business naming
- Business definitions
- Context building
- OKF-style structured context
- PostgreSQL persistence
- Vector persistence
- Neo4j knowledge graph
- Conversation persistence
- Superset integration
- Tool registry
- Mostly manual workflows with limited agent autonomy

The new application extends that foundation into a complete agentic analytics environment.

A user should be able to create a workspace, select one or more data sources and tables, provide a business objective, and allow the system to autonomously:

1. Understand the business context.
2. Profile the data.
3. Detect quality problems.
4. Discover relationships.
5. Generate analytical questions and hypotheses.
6. Perform iterative data analysis.
7. Perform statistical analysis and machine learning where justified.
8. Integrate data from multiple sources.
9. Create only the minimum ETL/ELT required.
10. Create reusable analytical datasets.
11. Create semantic models and KPIs.
12. Generate validated insights.
13. Design dashboards and reports.
14. Publish to Superset, Power BI, or other configurable analytics tools.
15. Schedule reports and recurring analysis.
16. Monitor metrics continuously.
17. Learn from prior analyses, dashboards, reports, and user feedback.
18. Allow users to intervene at any point.

The long-term product should behave like a virtual analytics organization.

---

# 2. Product Vision

## 2.1 Vision Statement

Build an enterprise analytics operating system that can convert raw enterprise data into validated business intelligence with minimal manual effort.

The target workflow is:

```text
Business Question
    ↓
Context Understanding
    ↓
Data Discovery
    ↓
Data Profiling
    ↓
Hypothesis Generation
    ↓
Exploratory Analysis
    ↓
Statistical Validation
    ↓
Data Integration / Transformation
    ↓
Semantic Modeling
    ↓
Insight Generation
    ↓
Visualization Design
    ↓
Dashboard / Report Publishing
    ↓
Scheduling / Monitoring
    ↓
Continuous Learning
```

The system must not behave like a simple “text-to-SQL” or “AI dashboard builder.”

It must reproduce the iterative working style of experienced analysts and data scientists.

---

# 3. Strategic Product Positioning

Context2AI AnalystOS should be positioned as:

> **An autonomous Data & Analytics Agent OS that combines enterprise business context, multi-agent reasoning, statistical analysis, dynamic data engineering, semantic modeling, BI automation, and continuous monitoring.**

The product story can be:

```text
Context2AI
    ↓
UNDERSTAND THE ENTERPRISE

AnalystOS
    ↓
ANALYZE THE ENTERPRISE

Future AgentOS
    ↓
ACT ON THE ENTERPRISE
```

---

# 4. Core Product Goals

## 4.1 Primary Goals

The platform must:

- Understand enterprise business and technical context.
- Work with one or more data sources.
- Minimize unnecessary ETL/ELT.
- Perform iterative analyst-style investigation.
- Perform data-scientist-style statistical analysis.
- Build reproducible, validated findings.
- Create reusable analytical datasets.
- Build semantic models.
- Generate dashboards and reports dynamically.
- Publish to pluggable BI/reporting platforms.
- Maintain complete lineage.
- Allow users to intervene at any time.
- Store all generated work.
- Support recurring and proactive analysis.
- Scale to large enterprise datasets.
- Apply governance, security, and authorization.
- Support model routing and multiple LLM providers.
- Support dynamic tools and reusable skills.
- Be observable and debuggable.

## 4.2 Non-Goals for Initial MVP

The MVP will not initially attempt to:

- Replace every enterprise ETL platform.
- Replace every data warehouse.
- Replace MDM.
- Replace enterprise IAM.
- Train foundation models.
- Build a universal BI rendering engine from scratch.
- Support all BI platforms on day one.
- Automatically change production source systems.
- Allow unrestricted agent access to sensitive enterprise data.

---

# 5. User Personas

## 5.1 Business User

Needs answers, dashboards, reports, and monitoring without writing SQL.

## 5.2 Data Analyst

Needs accelerated profiling, analysis, charting, metric creation, dashboard building, and ad hoc investigation.

## 5.3 Data Engineer

Needs automated source discovery, ingestion planning, minimum ETL/ELT, transformation generation, data validation, and optimization.

## 5.4 Data Scientist

Needs automated exploratory analysis, hypothesis creation, statistical testing, feature analysis, model experimentation, and insight validation.

## 5.5 BI Developer

Needs semantic datasets, chart recommendations, dashboard layout generation, report scheduling, and publishing automation.

## 5.6 Data Steward / Governance Team

Needs visibility into data access, lineage, business definitions, sensitive fields, tool usage, and publication.

## 5.7 Platform Administrator

Needs agent configuration, model routing, tool registry management, observability, cost management, policy management, and tenant administration.

---

# 6. Product Design Principles

1. **Context before generation**  
   Agents should understand business context before analyzing data.

2. **Tools before hallucination**  
   Calculations should be performed using deterministic tools.

3. **Evidence before insight**  
   Every insight must have reproducible evidence.

4. **Minimum ETL**  
   Do not build pipelines unless necessary.

5. **Push compute to the data**  
   Avoid extracting massive datasets when SQL pushdown is possible.

6. **Agent + Skill + Tool separation**  
   Do not create one agent for every capability.

7. **Human controllability**  
   Users can inspect, intervene, approve, reject, and redirect.

8. **Artifact persistence**  
   Every query, dataset, metric, dashboard, report, model, and finding must be stored.

9. **Verification as a default**  
   Important findings must be independently evaluated and verified.

10. **Platform independence**  
    BI/reporting destinations must be adapter-driven.

11. **Cost-aware execution**  
    Model, query, and compute cost must be managed.

12. **Enterprise governance by design**  
    Security cannot depend on prompts.

---

# 7. High-Level Architecture

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                              USER EXPERIENCE                                │
│ Chat | Workspace | Analysis Canvas | Dashboard Studio | Admin | APIs       │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          ANALYTICS AGENT OS                                  │
│                                                                             │
│  Supervisor / Planner                                                       │
│  Context Agent                                                              │
│  Metadata Agent                                                             │
│  Profiler Agent                                                             │
│  Data Quality Agent                                                         │
│  Investigation / Hypothesis Agent                                           │
│  Data Scientist Agent                                                       │
│  Statistical Agent                                                          │
│  Integration Agent                                                          │
│  Transformation Agent                                                       │
│  SQL Agent                                                                  │
│  Semantic Model Agent                                                       │
│  Insight Agent                                                              │
│  Visualization Agent                                                        │
│  BI Publisher Agent                                                         │
│  REV / Critic Agent                                                         │
│  Governance Agent                                                           │
└───────────────────┬──────────────────────────────┬──────────────────────────┘
                    │                              │
                    ▼                              ▼
           ┌──────────────────┐            ┌─────────────────────┐
           │   Tool Registry  │            │    Model Router     │
           └─────────┬────────┘            │ OpenRouter / Native │
                     │                     └─────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SKILL / TOOL LAYER                                 │
│ SQL | Python | Polars | Pandas | DuckDB | SciPy | Statsmodels | sklearn    │
│ Spark | Trino | Visualization | Superset | Power BI | ServiceNow | REST    │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           DATA EXECUTION PLANE                               │
│ Source Pushdown | In-Memory | Cache | Analytical DB | Materialized Dataset │
│ PostgreSQL | DuckDB | Redis | Spark | Trino | Warehouse                    │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                DATA SOURCES                                  │
│ DB | SaaS | API | Files | Lake | Warehouse | Streaming                     │
└─────────────────────────────────────────────────────────────────────────────┘

Context & Memory:
- PostgreSQL
- pgvector or external vector database
- Neo4j
- Object storage
- Existing Context2AI services
```

---

# 8. Core Platform Modules

The application should be divided into the following platform modules:

1. Workspace Service
2. Agent Runtime
3. Workflow / Orchestration Engine
4. Context Service Adapter
5. Metadata Service
6. Data Access Gateway
7. Query Execution Engine
8. Python / Data Science Sandbox
9. Data Integration Engine
10. Transformation Engine
11. Semantic Model Service
12. Insight Service
13. Artifact Registry
14. BI Publishing Gateway
15. Scheduler
16. Monitoring Engine
17. Tool Registry
18. Skill Registry
19. Model Router
20. Policy / Governance Engine
21. Memory Service
22. Lineage Service
23. Observability Service
24. Cost Management Service
25. Notification Service
26. Admin Console

---

# 9. Workspace Model

The workspace is the primary unit of organization.

Example:

```text
Workspace:
    ServiceNow Incident Intelligence

Objective:
    Identify operational gaps and drivers of SLA breach.

Sources:
    servicenow_incident
    servicenow_change_request
    cmdb_ci
    employee
    application_inventory

Artifacts:
    45 queries
    11 hypotheses
    18 findings
    5 datasets
    2 semantic models
    3 dashboards
    2 reports
    4 schedules
```

## 9.1 Workspace Capabilities

A workspace must support:

- Name
- Description
- Business objective
- One or more data sources
- Selected schemas/tables/views/files/APIs
- Context packages
- Business terminology
- Data access rules
- Agent autonomy mode
- Model-routing configuration
- Tool permissions
- Cost limits
- Query limits
- User collaborators
- Agent execution history
- Artifacts
- Schedules
- Insights
- Notifications
- Audit history

---

# 10. Workspace State Model

Recommended logical state:

```json
{
  "workspace": {},
  "objective": {},
  "sources": [],
  "business_context": {},
  "metadata": {},
  "relationships": [],
  "profiles": {},
  "quality_findings": [],
  "questions": [],
  "hypotheses": [],
  "experiments": [],
  "queries": [],
  "transformations": [],
  "datasets": [],
  "semantic_models": [],
  "metrics": [],
  "insights": [],
  "visualizations": [],
  "dashboards": [],
  "reports": [],
  "models": [],
  "schedules": [],
  "agent_runs": [],
  "human_feedback": [],
  "decisions": [],
  "approvals": []
}
```

---

# 11. Context2AI Integration

The second application should not rebuild Context2AI.

Context2AI should become a reusable enterprise context service.

## 11.1 Recommended Context APIs

```text
GET /context/workspaces/{id}
GET /context/entities
GET /context/tables
GET /context/columns
GET /context/relationships
GET /context/metrics
GET /context/business-rules
GET /context/lineage
POST /context/search
POST /context/graph/query
POST /context/enrich
```

## 11.2 Context Types

The system should retrieve:

- Technical metadata
- Business names
- Business definitions
- Synonyms
- Domain terminology
- Data owners
- Table relationships
- Column relationships
- Metrics
- KPI definitions
- Calculation rules
- Data quality rules
- Existing dashboards
- Existing reports
- Prior queries
- Known business questions
- Known anomalies
- Known filters
- Access classifications
- PII tags
- Sensitive-data tags
- Known lineage

## 11.3 Context Retrieval Strategy

Agents should use layered retrieval:

1. Exact metadata
2. Graph neighborhood
3. Semantic vector search
4. Prior artifacts
5. Conversation memory
6. User-provided context
7. Source inspection when information is still missing

---

# 12. Agent Runtime Design

## 12.1 Agent Definition

Agents should be configuration-driven.

Example:

```yaml
agent:
  id: data_scientist
  version: 1.0
  description: Performs statistical and machine-learning investigation.

  capabilities:
    - profiling
    - segmentation
    - statistical_testing
    - clustering
    - regression
    - classification
    - anomaly_detection
    - forecasting

  skills:
    - dataframe_analysis
    - hypothesis_testing
    - feature_importance
    - time_series_analysis

  tools:
    - sql_executor
    - python_executor
    - dataframe_engine
    - artifact_writer

  model_profile:
    type: analytical_reasoning

  policies:
    max_rows_extract: 500000
    pii_access: restricted
    approval_for_publish: true

  verification:
    required: true
```

## 12.2 Agent Execution Lifecycle

```text
NEW
 ↓
PLANNING
 ↓
READY
 ↓
RUNNING
 ↓
WAITING_TOOL / WAITING_USER
 ↓
EVALUATING
 ↓
VERIFYING
 ↓
COMPLETED / FAILED / REJECTED
```

### Execution invariants

- Resolve the caller's workspace, source, asset, row/column, and action permissions on the server. Carry the resulting scope through planning and retrieval, then enforce it again in the data gateway for every execution path, including generated SQL, submitted SQL, repairs, fallbacks, and tool calls. Context filtering and prompt instructions are not authorization.
- Treat catalog descriptions, retrieved documents, source values, and user-supplied files as untrusted input. They may inform analysis but cannot change system policy, grant a tool, or expand the authorized scope. SQL identifiers must resolve to allowed catalog assets before execution.
- Persist the exact plan, plan version, input references, policy version, and authorized scope with the run. Replanning or changing any approval-relevant input invalidates affected approvals and requires a fresh policy check.
- Recheck authorization and policy at each step, including after a pause or approval wait. A permission revoked during a run must block the next protected action.

---

# 13. Core Persistent Agent Catalog

## 13.1 Analytics Supervisor

Responsibilities:

- Interpret user objective
- Build execution plan
- Break work into tasks
- Assign specialized agents
- Resolve dependencies
- Manage re-planning
- Track progress
- Consolidate results
- Escalate for human input

## 13.2 Context Agent

Responsibilities:

- Retrieve Context2AI context
- Resolve terminology
- Map business concepts to technical fields
- Retrieve known metrics
- Retrieve known dashboards/reports
- Enrich missing context
- Identify ambiguity

## 13.3 Metadata Agent

Responsibilities:

- Discover schemas, tables, views
- Discover columns and types
- Discover keys and indexes
- Discover partitions
- Discover row counts
- Discover source statistics
- Discover freshness
- Identify candidate relationships

## 13.4 Dataset Profiler Agent

Responsibilities:

- Cardinality
- Null rate
- Distinct values
- Min/max
- Quantiles
- Histograms
- Category frequency
- Skewness
- Outlier candidates
- Duplicate detection
- Time coverage
- Basic correlation

## 13.5 Data Quality Agent

Responsibilities:

- Missing values
- Invalid values
- Duplicates
- Broken joins
- Referential integrity
- Unexpected categories
- Schema drift
- Freshness
- Late-arriving data
- Timestamp anomalies
- Business-rule violations

## 13.6 Relationship Discovery Agent

Responsibilities:

- Infer joins
- Compare key distributions
- Detect many-to-many risks
- Match business entities
- Validate joins
- Write relationships to graph
- Detect semantic relationship conflicts

## 13.7 Investigation / Hypothesis Agent

Responsibilities:

- Convert goals into analytical questions
- Generate hypotheses
- Prioritize hypotheses
- Identify required evidence
- Suggest analytical methods
- Manage hypothesis status
- Generate follow-up questions

## 13.8 Data Scientist Agent

Responsibilities:

- Exploratory analysis
- Segmentation
- Cohort analysis
- Correlation
- Statistical testing
- Classification
- Regression
- Clustering
- Feature importance
- Anomaly detection
- Forecasting
- Time-series decomposition

## 13.9 Data Integration Agent

Responsibilities:

- Multi-source join planning
- Key discovery
- Type alignment
- Data normalization
- Deduplication
- Entity matching
- Federation vs materialization decisions

## 13.10 Transformation Agent

Responsibilities:

- Generate minimal required transformations
- Create reusable datasets
- Build SQL transformations
- Build Python/Polars transformations
- Build materializations
- Build incremental transformations

## 13.11 SQL Engineer Agent

Responsibilities:

- Generate SQL
- Correct SQL
- Convert SQL dialects
- Optimize SQL
- Analyze execution plans
- Reduce scans
- Apply predicate pushdown
- Recommend indexing where appropriate

## 13.12 Semantic Model Agent

Responsibilities:

- Define measures
- Define dimensions
- Define grains
- Define relationships
- Define filters
- Define date dimensions
- Define KPI logic
- Detect metric duplication

## 13.13 Insight Analyst Agent

Responsibilities:

- Convert validated results into business findings
- Attach evidence
- Estimate scope/impact
- Identify caveats
- Build executive narrative
- Track insight confidence

## 13.14 Visualization Agent

Responsibilities:

- Select chart type
- Configure visual encoding
- Determine drill paths
- Choose filters
- Select audience-specific visual style
- Ensure visual consistency

## 13.15 BI Publisher Agent

Responsibilities:

- Publish datasets
- Create charts
- Create dashboards
- Create reports
- Apply layout
- Configure filters
- Configure refresh
- Configure schedules
- Update existing BI artifacts

## 13.16 REV / Critic Agent

REV = Reason → Evaluate → Verify

Responsibilities:

- Challenge conclusions
- Verify reproducibility
- Check data sufficiency
- Check semantic correctness
- Re-run selected analysis
- Detect contradictory findings
- Validate evidence

## 13.17 Governance Agent

Responsibilities:

- Check access rules
- Enforce sensitive-data policy
- Block restricted publication
- Check destination permissions
- Enforce masking
- Enforce row/column policies
- Record audit trail

---

# 14. Skills Model

Agents should use reusable skills.

Example categories:

## 14.1 Data Understanding Skills

- schema_discovery
- metadata_summary
- relationship_detection
- column_semantics
- business_term_resolution

## 14.2 Data Profiling Skills

- numeric_profile
- categorical_profile
- datetime_profile
- outlier_detection
- uniqueness_analysis
- missingness_analysis

## 14.3 Analysis Skills

- descriptive_statistics
- cohort_analysis
- pareto_analysis
- segmentation
- trend_analysis
- seasonality_analysis
- funnel_analysis
- retention_analysis
- root_cause_analysis
- variance_analysis

## 14.4 Statistical Skills

- correlation
- chi_square
- t_test
- anova
- nonparametric_test
- regression
- logistic_regression
- confidence_interval
- significance_testing

## 14.5 ML Skills

- clustering
- classification
- regression_ml
- anomaly_detection
- feature_importance
- dimensionality_reduction
- forecasting

## 14.6 BI Skills

- KPI_design
- chart_selection
- dashboard_layout
- executive_dashboard
- operational_dashboard
- report_generation
- scheduled_report

## 14.7 Engineering Skills

- query_optimization
- data_federation
- data_materialization
- incremental_processing
- join_optimization
- cache_selection

---

# 15. Tool Registry

Each tool should have:

```json
{
  "tool_id": "superset.create_dashboard",
  "name": "Create Superset Dashboard",
  "version": "1.0",
  "description": "Creates a dashboard in Superset.",
  "capabilities": [
    "dashboard",
    "chart",
    "filters"
  ],
  "input_schema": {},
  "output_schema": {},
  "permission_scope": "workspace",
  "risk": "medium",
  "cost_profile": "low",
  "latency_profile": "medium",
  "approval_policy": "publish_only",
  "runtime": "remote_api"
}
```

## 15.1 Tool Categories

- Metadata tools
- SQL tools
- Dataframe tools
- Statistics tools
- ML tools
- Visualization tools
- BI publishing tools
- Scheduling tools
- Notification tools
- Data-quality tools
- Graph tools
- Vector-search tools
- Context tools
- File tools
- API tools
- Governance tools
- Observability tools

---

# 16. Data Access Gateway

All data access should go through a controlled gateway.

Capabilities:

- Connection pooling
- Credential abstraction
- Secret retrieval
- Read-only enforcement
- Query timeout
- Row limits
- Query cost controls
- SQL validation
- Query fingerprinting
- Query cache
- Dialect support
- Sampling support
- Audit logging

Initial connectors:

- PostgreSQL
- SQL Server
- Oracle
- MySQL
- Snowflake
- BigQuery
- Databricks
- Trino
- ServiceNow
- REST API
- CSV
- Parquet
- Excel
- Object storage

---

# 17. Minimum ETL / ELT Strategy

The system should prefer the least expensive data movement option.

## 17.1 Decision Matrix

| Scenario | Preferred Engine |
|---|---|
| Small local dataset | Pandas / Polars |
| Medium analytical dataset | DuckDB / Polars |
| Large source already queryable | Push SQL to source |
| Multiple queryable sources | Trino / federation |
| Repeated expensive analysis | Cache |
| Reusable transformed dataset | Materialize |
| Very large distributed workload | Spark |
| Long-lived governed pipeline | ETL/ELT orchestration |

## 17.2 Decision Inputs

- Dataset size
- Number of sources
- Join complexity
- Query frequency
- Source capability
- Network cost
- Expected reuse
- Freshness requirement
- Security policy
- Data locality
- Compute availability
- SLA

---

# 18. Analytical Data Workspace

The workspace should expose a logical dataframe abstraction.

Example:

```python
dataset = workspace.load("servicenow.incident")
```

The runtime may execute this using:

- SQL pushdown
- Polars
- Pandas
- DuckDB
- Spark
- Trino
- Warehouse SQL

The caller should not need to know initially.

---

# 19. Analyst / Data Scientist Iteration Loop

The platform should mimic expert analytical iteration:

```text
Understand
 ↓
Profile
 ↓
Classify
 ↓
Slice
 ↓
Compare
 ↓
Hypothesize
 ↓
Test
 ↓
Transform
 ↓
Reclassify
 ↓
Validate
 ↓
Refine
 ↓
Conclude
```

The system should support repeated loops instead of a one-shot query.

---

# 20. Hypothesis Management

Hypothesis object:

```json
{
  "hypothesis_id": "H-102",
  "statement": "High reassignment count drives SLA breach.",
  "priority": "high",
  "status": "testing",
  "required_data": [],
  "methods": [
    "segmentation",
    "logistic_regression"
  ],
  "evidence": [],
  "confidence": null,
  "conclusion": null
}
```

Statuses:

- proposed
- approved
- testing
- supported
- rejected
- inconclusive
- superseded

---

# 21. Statistical and Data Science Capability

The platform should support:

## 21.1 Descriptive Analysis

- Counts
- Ratios
- Mean
- Median
- Standard deviation
- Percentiles
- Distribution
- Trend
- Moving average
- Growth rates

## 21.2 Statistical Analysis

- Pearson correlation
- Spearman correlation
- Chi-square
- T-test
- ANOVA
- Mann-Whitney
- Kruskal-Wallis
- Confidence intervals
- Logistic regression
- Linear regression

## 21.3 Machine Learning

- Classification
- Regression
- Clustering
- Decision trees
- Random forests
- Gradient boosting
- Feature importance
- Anomaly detection

## 21.4 Time Series

- Trend decomposition
- Seasonality
- Change-point detection
- Forecasting
- Rolling metrics
- Outlier detection

## 21.5 Business Analysis

- Pareto
- Cohort analysis
- Funnel analysis
- Segmentation
- SLA analysis
- Queue analysis
- Aging analysis
- Root cause
- Driver analysis

---

# 22. Sampling Strategy

For large datasets:

```text
Sample
 ↓
Explore
 ↓
Identify promising hypothesis
 ↓
Increase sample
 ↓
Validate
 ↓
Full-data confirmation
```

Sampling techniques:

- Random
- Stratified
- Time-window
- Category-balanced
- Reservoir
- Top-N
- Anomaly-focused

---

# 23. Query Optimization

The SQL agent must support:

- Predicate pushdown
- Partition pruning
- Column pruning
- Join order
- Join cardinality analysis
- Aggregation pushdown
- CTE optimization
- Temporary-table strategy
- Materialization decisions
- Incremental aggregation
- Statistics review
- Execution-plan comparison

---

# 24. Semantic Model

Each metric should include:

```json
{
  "metric": "MTTR",
  "display_name": "Mean Time to Resolve",
  "definition": "Average resolved_time - opened_time for resolved incidents.",
  "grain": "incident",
  "filters": [
    "state = resolved"
  ],
  "dimensions": [
    "assignment_group",
    "application",
    "severity",
    "region"
  ],
  "owner": "IT Operations",
  "source_columns": [],
  "validation": {}
}
```

Semantic model capabilities:

- Metrics
- Dimensions
- Hierarchies
- Relationships
- Calculated measures
- Derived dimensions
- Date logic
- Grain
- Filters
- Business definitions
- Versioning
- Approval status

---

# 25. Insight Object

Every finding must be persisted.

Example:

```json
{
  "insight_id": "I-984",
  "finding": "Incidents reassigned more than twice have a materially higher SLA breach rate.",
  "confidence": 0.94,
  "population_size": 1874312,
  "evidence": [
    "query_183",
    "experiment_18",
    "chart_42"
  ],
  "business_impact": {
    "tickets": 18241
  },
  "caveats": [],
  "verified": true
}
```

---

# 26. REV Verification Model

## 26.1 Reason

- What is being claimed?
- What question does it answer?
- What evidence is required?

## 26.2 Evaluate

- Is the method appropriate?
- Is the sample representative?
- Is the result statistically meaningful?
- Does the conclusion overreach?

## 26.3 Verify

- Can the query be rerun?
- Can the analysis be reproduced?
- Does a second method support the result?
- Does a second model agree on interpretation?
- Are there contradictory signals?

---

# 27. Model Router

Use a model abstraction layer.

Possible providers:

- OpenAI
- Anthropic
- Google
- Other approved enterprise models
- Internal/self-hosted models

Routing can be task-based.

Example:

```yaml
routing:
  planning:
    profile: reasoning_strong

  sql:
    profile: coding

  statistical_interpretation:
    profile: analytical_reasoning

  summarization:
    profile: low_cost

  verification:
    profile: independent_model_family
```

Model router requirements:

- Provider abstraction
- Fallback
- Timeout
- Retry
- Cost limits
- Token limits
- Per-workspace policy
- Logging
- Redaction
- Model allowlist
- Region constraints

---

# 28. Multi-Model Verification

For critical findings:

```text
Primary Agent
    ↓
Primary Model
    ↓
Evidence
    ↓
Independent Critic
    ↓
Different Model Family
    ↓
Agreement / Disagreement
```

The system should not assume model agreement equals truth. Final verification must still be grounded in reproducible data evidence.

---

# 29. Memory Architecture

Four memory types are required.

## 29.1 Working Memory

Current execution context.

## 29.2 Episodic Memory

Prior agent runs and decisions.

## 29.3 Semantic Memory

Business context and learned domain knowledge.

## 29.4 Artifact Memory

Queries, datasets, dashboards, reports, metrics, models, and schedules.

Storage recommendation:

| Memory | Storage |
|---|---|
| Working state | PostgreSQL / Redis |
| Episodic | PostgreSQL |
| Semantic | Vector DB + PostgreSQL |
| Relationships | Neo4j |
| Large artifacts | Object store |
| Cache | Redis |

---

# 30. Analytics Knowledge Graph

Neo4j should represent:

```text
Business Objective
   ↓ asks
Business Question
   ↓ tested_by
Hypothesis
   ↓ supported_by
Evidence
   ↓ derived_from
Dataset
   ↓ built_from
Table / Column
   ↓ represented_by
Metric
   ↓ visualized_by
Chart
   ↓ included_in
Dashboard
```

The graph should also link:

- Context
- Source system
- User
- Agent
- Tool
- Query
- Dataset
- Report
- Schedule
- Decision
- Finding

---

# 31. Artifact Registry

Artifact types:

- Query
- Notebook
- Python script
- Transformation
- Dataset
- Semantic model
- Metric
- Chart
- Dashboard
- Report
- ML model
- Forecast
- Alert
- Schedule
- Export
- Narrative
- Data-quality rule

Suggested fields:

```text
artifact_id
workspace_id
artifact_type
name
version
platform
external_id
creator_agent
creator_user
created_at
updated_at
status
dependencies
lineage
source_hash
configuration
location
schedule
```

---

# 32. BI Publishing Abstraction

Create a platform-independent BI publisher.

Interface:

```text
create_dataset()
update_dataset()
create_metric()
create_chart()
update_chart()
create_dashboard()
update_dashboard()
publish_dashboard()
create_report()
schedule_report()
create_alert()
export_artifact()
```

Adapters:

- Superset
- Power BI
- Tableau
- Looker
- Grafana
- Custom report engine

Initial release should prioritize Superset.

Power BI should be phase 2.

---

# 33. Dashboard Designer

Dashboard generation inputs:

- Audience
- Objective
- Metrics
- Insights
- Time grain
- Dataset characteristics
- Interactivity requirements
- Drill-down requirements
- Existing style/template

## 33.1 Executive Dashboard Pattern

Typical structure:

- KPI cards
- Trend
- Major drivers
- Top risk
- Forecast
- Executive summary
- Recommended action

## 33.2 Operational Dashboard Pattern

Typical structure:

- Filters
- Drill-downs
- Heat maps
- Aging
- Queue
- Detailed table
- SLA status
- Operational exceptions

---

# 34. Visualization Rules

Examples:

| Data Intent | Default Visual |
|---|---|
| Trend | Line |
| Category comparison | Bar |
| Distribution | Histogram |
| Relationship | Scatter |
| Part-to-whole | Stacked bar / treemap |
| Geography | Map |
| Process flow | Sankey |
| Operational detail | Table |
| Correlation | Heatmap |
| KPI | KPI card |
| Aging | Bucketed bar |
| Cohort | Cohort heatmap |

The visualization agent should override defaults when context justifies it.

---

# 35. Existing Dashboard / Report Mode

User can provide or select an existing dashboard.

System should:

1. Inspect dashboard metadata.
2. Identify datasets.
3. Identify metrics.
4. Identify queries.
5. Identify filters.
6. Identify layout.
7. Map dashboard to workspace context.
8. Accept a requested change.
9. Generate required analysis.
10. Update or create a new version.
11. Maintain lineage.

---

# 36. Reporting

Supported report outputs:

- Executive report
- Operational report
- Daily summary
- Weekly summary
- Monthly review
- PDF
- PowerPoint
- Excel
- CSV
- Narrative report
- Exception report
- Incident report
- Statistical report

Report generation should be template-driven and agent-assisted.

---

# 37. Scheduling

Schedules should support:

- Dataset refresh
- Dashboard refresh
- Report generation
- Re-analysis
- Anomaly detection
- Insight refresh
- Email delivery
- Slack / Teams delivery
- Webhook
- File export

Example:

```text
Every Monday 7 AM
    ↓
Refresh Incident Data
    ↓
Compare with previous week
    ↓
Detect meaningful change
    ↓
Update findings
    ↓
Refresh dashboard
    ↓
Generate leadership summary
    ↓
Publish / deliver
```

---

# 38. Continuous Analytics

Long-term capability:

- Metric drift detection
- Data quality drift
- Schema drift
- Statistical anomaly detection
- New clusters
- New correlations
- Emerging patterns
- Forecast deviations
- Business threshold breaches

A proactive agent may create a new analytical investigation automatically when policy allows.

---

# 39. Human-in-the-Loop

Autonomy modes:

| Level | Behavior |
|---|---|
| 0 | Manual |
| 1 | Agent recommends |
| 2 | Agent executes after approval |
| 3 | Agent executes; publish requires approval |
| 4 | Fully autonomous within policy |

Level 4 is a bounded operating mode, not blanket authority. It must be explicitly enabled for named action types, sources/assets, destinations, and budgets. The initial release remains read-only against source systems; publication, scheduling, external notification, and source mutation require an approval unless a separately reviewed policy explicitly authorizes that exact action class. The system must expose the effective autonomy ceiling and the reason an action was allowed, denied, or held.

An approval is bound to an immutable action proposal: requester and approver, risk tier, action, source and destination, affected assets, proposed changes or publication payload, plan hash, policy version, and expiration. Execution must verify the hash and re-check current authorization immediately before the action. Any replan or payload/scope change requires a new approval. Where separation of duties is configured, the requester cannot approve their own action.

Users must be able to:

- Pause
- Resume
- Cancel
- Redirect
- Add context
- Reject finding
- Edit metric
- Edit hypothesis
- Add filters
- Request deeper analysis
- Approve publication
- Roll back publication

---

# 40. Dynamic Replanning

If the user interrupts:

> Ignore network incidents. Focus only on application incidents.

The supervisor should:

1. Persist user instruction.
2. Mark impacted tasks.
3. Cancel invalid pending tasks.
4. Recompute dependencies.
5. Reuse still-valid artifacts.
6. Replan the remaining work.
7. Continue execution.

---

# 41. Workflow / Orchestration

Recommended orchestration requirements:

- Durable state
- Long-running workflows
- Retries
- Timeout
- Compensation
- Human wait states
- Child workflows
- Parallel tasks
- Task priority
- Idempotency
- Event-driven resume

Candidate technologies:

- Temporal
- LangGraph with durable persistence
- Custom workflow layer over Temporal

Recommended approach:

> Use Temporal for durable enterprise workflow execution and optionally use LangGraph inside analytical agent flows.

Runtime requirements:

- Execute long-running work asynchronously and expose persisted status/progress events so the user can pause, redirect, or cancel without holding a request or database transaction open across model calls.
- Persist one durable unit per workflow step. Give each step a stable idempotency key so retries, duplicate dispatch, and worker recovery do not repeat side effects.
- Keep approval waits durable. Resume only the approved, hash-matching plan; a changed plan returns to policy evaluation and approval.
- Retry transient failures with bounded backoff. Do not retry non-idempotent publication or mutation unless the destination supports idempotency or the workflow first reconciles the prior outcome. Define compensation or a manual recovery state for partial external side effects.
- A disconnected client must not silently leave work running when the user requested cancellation. Record whether a run was cancelled, completed, or stopped after a partial side effect.

---

# 42. Event Model

Recommended events:

```text
workspace.created
workspace.updated
source.connected
metadata.collected
context.loaded
profile.completed
quality.issue.detected
hypothesis.created
hypothesis.updated
query.executed
analysis.completed
insight.created
insight.verified
dataset.created
metric.created
dashboard.created
dashboard.published
report.generated
schedule.executed
agent.started
agent.completed
agent.failed
approval.requested
approval.completed
policy.denied
```

---

# 43. API Surface

## 43.1 Workspace

```text
POST   /api/workspaces
GET    /api/workspaces/{id}
PATCH  /api/workspaces/{id}
DELETE /api/workspaces/{id}
```

## 43.2 Sources

```text
POST /api/workspaces/{id}/sources
GET  /api/workspaces/{id}/sources
POST /api/workspaces/{id}/sources/{source_id}/discover
```

## 43.3 Analysis

```text
POST /api/workspaces/{id}/analysis
POST /api/workspaces/{id}/analysis/{run_id}/pause
POST /api/workspaces/{id}/analysis/{run_id}/resume
POST /api/workspaces/{id}/analysis/{run_id}/cancel
POST /api/workspaces/{id}/analysis/{run_id}/feedback
GET  /api/workspaces/{id}/analysis/{run_id}
GET  /api/workspaces/{id}/analysis/{run_id}/events   # persisted event stream (SSE or equivalent)
```

## 43.4 Artifacts

```text
GET  /api/workspaces/{id}/artifacts
GET  /api/artifacts/{artifact_id}
POST /api/artifacts/{artifact_id}/publish
POST /api/artifacts/{artifact_id}/approve
```

## 43.5 Dashboards

```text
POST /api/workspaces/{id}/dashboards
POST /api/dashboards/{id}/publish
POST /api/dashboards/{id}/schedule
```

## 43.6 Context

```text
POST /api/context/search
GET  /api/context/entities/{id}
```

## 43.7 Tools

```text
GET  /api/tools
POST /api/tools
PATCH /api/tools/{id}
```

## 43.8 Agents

```text
GET  /api/agents
POST /api/agents
PATCH /api/agents/{id}
GET  /api/agent-runs/{id}
```

---

# 44. Suggested PostgreSQL Logical Schema

Core tables:

```text
workspace
workspace_member
workspace_source
workspace_policy

agent_definition
agent_run
agent_task
agent_message
agent_decision

tool_definition
tool_execution

skill_definition
skill_execution

context_reference
memory_episode

dataset
dataset_version
dataset_column
dataset_relation

query
query_execution

hypothesis
experiment
analysis_result
insight
evidence

semantic_model
metric
dimension

artifact
artifact_version
artifact_dependency
artifact_lineage

dashboard
dashboard_version
report
report_version

schedule
schedule_run

approval
feedback

audit_event
cost_event
```

---

# 45. Security Architecture

Security must be enforced outside the LLM.

Controls:

- SSO
- RBAC
- ABAC
- Workspace roles
- Source-level permissions
- Table-level permissions
- Column-level permissions
- Row-level security
- PII masking
- Secrets management
- Tool allowlisting
- Model allowlisting
- Export controls
- Network policy
- Data residency policy
- Audit logs
- Approval controls

Enforcement requirements:

- The application control-plane database and user-query execution identity must be separated. User/model SQL must not be able to read application credentials, users, audit records, or other control-plane tables.
- Enforce authorization at the execution boundary, not only in the UI, planner, or retrieved context. Scope checks apply to direct API calls and agent/tool execution alike.
- Never place source credentials or secret values in prompts, conversation memory, cached artifacts, or model-call logs. Resolve secrets just in time through the configured secret manager and use least-privilege connector identities.
- Provider routing, residency, and model allowlists are policy inputs. If a required provider is unavailable, fail with a visible error; do not silently route sensitive data to an unapproved provider.

---

# 46. Governance

The governance engine must evaluate:

```text
Who is the user?
Which workspace?
Which agent?
Which data?
Which columns?
Which tool?
Which destination?
For what purpose?
What output?
```

The decision record must also include the effective workspace membership/permission, resolved data scope, action risk, model/provider route, applicable residency rule, and budget. Return one of **allow**, **deny**, or **approval required** with machine-readable reasons. Re-evaluate the decision at execution time; a prior planning or retrieval decision cannot authorize a later action by itself.

Example execution identity:

```text
user: sam
workspace: ws_238
agent: data_scientist
purpose: incident_analysis
tool: sql_executor
source: servicenow_prod
```

---

# 47. Agent Sandbox

Python/data-science execution must run in a controlled sandbox.

Controls:

- CPU quota
- Memory quota
- Timeout
- Package allowlist
- File-system isolation
- Network restriction
- Dataset size limits
- Artifact output directory
- Logging
- Process cleanup

---

# 48. Performance Architecture

Performance controls:

- Sampling
- Query pushdown
- Result caching
- Query fingerprinting
- Column pruning
- Partition pruning
- Incremental execution
- Reuse prior artifacts
- Parallel task execution
- Query concurrency limits
- Background materialization
- Async BI publishing
- Execution-engine selection

Suggested engine routing:

```text
< 1 GB          → Polars / Pandas / DuckDB
1–50 GB         → DuckDB / source pushdown / Trino
> 50 GB         → source engine / Spark / warehouse
High reuse      → materialized analytical dataset
Cross-source    → Trino / temporary materialization
```

---

# 49. Caching Strategy

Cache levels:

1. Metadata cache
2. Context retrieval cache
3. SQL result cache
4. Dataframe cache
5. Profile cache
6. Semantic model cache
7. Dashboard metadata cache
8. LLM response cache where safe

Cache key should include:

- Query hash
- Source version
- User security scope
- Parameters
- Workspace
- Dataset version

---

# 50. Observability

Every execution should expose:

- Agent
- Task
- Model
- Prompt version
- Tool
- Input
- Output
- Query
- Runtime
- Token count
- LLM cost
- Compute cost
- Retry count
- Error
- Evidence
- Verification status
- Approval status

Dashboards for platform operations:

- Active runs
- Failed runs
- LLM cost
- Query cost
- Top tools
- Slow queries
- Agent failure rate
- Tool failure rate
- Model latency
- Publish failures
- User approvals
- Workspace utilization

---

# 51. Cost Management

Controls:

- Per-workspace token budget
- Per-run token budget
- Per-agent budget
- Max query scan
- Max extracted rows
- Max Python memory
- Max concurrent tasks
- Model tier policy
- Cheap-model fallback
- Expensive-model approval threshold

---

# 52. User Interface

Primary screens:

## 52.1 Workspace Home

- Objective
- Sources
- Context
- Current agent activity
- Key insights
- Artifacts
- Schedules

## 52.2 Data Explorer

- Sources
- Schemas
- Tables
- Profiles
- Quality
- Relationships
- Samples

## 52.3 Investigation

Tree view:

```text
Question
 ├── Hypothesis 1 ✓
 ├── Hypothesis 2 ✗
 ├── Hypothesis 3 Running
 │    ├── Query
 │    ├── Statistical Test
 │    └── Finding
 └── Hypothesis 4
```

## 52.4 Analysis Canvas

```text
Question
   ↓
Hypothesis
   ↓
SQL / Python
   ↓
Evidence
   ↓
Finding
   ↓
Metric
   ↓
Chart
   ↓
Dashboard
```

## 52.5 Insights

- Finding
- Evidence
- Confidence
- Business impact
- Caveats
- Verification status

## 52.6 Studio

- Queries
- Datasets
- Metrics
- Charts
- Dashboards
- Reports

## 52.7 Agent Console

- Plan
- Tasks
- Logs
- Tool calls
- Model calls
- Cost
- Errors
- Approval requests

## 52.8 Admin

- Agent registry
- Skill registry
- Tool registry
- Models
- Policies
- Secrets
- Destinations
- Cost controls

---

# 53. Example ServiceNow Use Case

User objective:

> Analyze Incident and Change Request data, identify operational gaps, determine drivers of SLA breach, and create executive and operational dashboards.

Agent flow:

1. Load ServiceNow context.
2. Discover incident and CR metadata.
3. Profile tables.
4. Discover joins.
5. Check data quality.
6. Generate analytical questions.
7. Generate hypotheses.
8. Test assignment-group performance.
9. Test after-hours patterns.
10. Test reassignment impact.
11. Test change-related incident patterns.
12. Test application-specific concentration.
13. Validate statistically.
14. Create reusable dataset.
15. Define KPIs.
16. Generate findings.
17. REV verification.
18. Create executive dashboard.
19. Create operational dashboard.
20. Publish to Superset.
21. Schedule weekly refresh.
22. Monitor for new anomalies.
23. Persist all lineage.

---

# 54. Example Findings

Possible generated findings:

- Assignment Group A has elevated SLA breach after multiple reassignments.
- Application X contributes disproportionately to Sev-1 volume.
- Change-related incidents spike after specific deployment windows.
- Incidents opened after business hours have significantly higher MTTR.
- Reassignment count is a strong predictor of SLA breach.
- Certain CIs repeatedly appear in high-severity incidents.

The system must not fabricate these. Each finding requires evidence.

---

# 55. Non-Functional Requirements

## Availability

- Control plane target: 99.9%
- Workflow durability required
- Tool failures isolated

## Scalability

- Multi-workspace
- Multi-tenant capable
- Horizontal agent workers
- Horizontal query workers
- Horizontal publishing workers

## Performance

Targets for MVP:

- Workspace creation: < 3 sec
- Context search: < 2 sec typical
- Metadata view: < 3 sec
- Agent status updates: near real-time
- Dashboard publish orchestration: < 30 sec excluding BI processing
- Query execution: source dependent

## Auditability

100% of:

- Agent executions
- Tool calls
- Queries
- Publications
- Approvals
- Policy decisions

must be auditable.

Availability and latency figures in this section are targets, not current service claims. Each deployment must publish measured SLOs, state which source/provider dependencies are included, and document the load profile and recovery evidence used to support them.

---

# 56. Testing Strategy

## 56.1 Unit Testing

- Agent planners
- Tool adapters
- SQL generation helpers
- Metric logic
- Policy rules
- Storage layer

## 56.2 Integration Testing

- Context2AI
- PostgreSQL
- Neo4j
- Vector DB
- Superset
- Source connectors
- Model router

## 56.3 Analytical Correctness Testing

Create benchmark datasets with known expected results.

Tests:

- Aggregation correctness
- Join correctness
- Statistical-test correctness
- Metric correctness
- Filter correctness
- Dashboard dataset correctness

## 56.4 Agent Evaluation

Evaluate:

- Task completion
- Tool selection
- SQL validity
- Hallucination rate
- Insight support
- Correct use of context
- Replanning quality
- Cost

Evaluation must distinguish evidence quantity from evidence correctness. Include adversarial paired cases where a valid and invalid proposal have similarly complete evidence, and measure false approvals by risk tier. For every risk tier, define a minimum evaluation corpus and an explicit release threshold before enabling unattended behavior. Model agreement or a high confidence score is not a substitute for deterministic checks and reproducible data evidence.

## 56.5 Security Testing

- SQL injection
- Prompt injection
- Data exfiltration
- Tool privilege escalation
- PII leakage
- Cross-tenant access
- Unauthorized publication
- Scope escape through joins, CTEs, aliases, tool calls, repair/fallback paths, or stale approvals
- Cross-workspace cache or artifact reuse
- Revocation while a run is paused or awaiting approval
- Duplicate dispatch, worker restart, and cancellation during an external side effect
- Provider fallback and data-residency policy enforcement

---

# 57. Deployment Architecture

Suggested initial deployment:

```text
Kubernetes / OpenShift

Services:
- web-ui
- api-gateway
- workspace-service
- agent-orchestrator
- agent-workers
- context-adapter
- metadata-service
- query-service
- python-sandbox
- semantic-service
- artifact-service
- publishing-service
- schedule-service
- policy-service
- observability-service

Infrastructure:
- PostgreSQL
- Redis
- Neo4j
- Object storage
- Kafka/NATS optional
- Temporal
```

---

# 58. Repository Structure

Suggested monorepo:

```text
/apps
  /web
  /api

/services
  /workspace
  /agent-runtime
  /context
  /metadata
  /query
  /analysis
  /semantic
  /artifact
  /publishing
  /scheduler
  /governance
  /observability

/agents
  /supervisor
  /context
  /profiler
  /investigator
  /data_scientist
  /sql
  /semantic
  /publisher
  /critic

/skills
/tools
/connectors
/bi-adapters
/models
/workflows
/shared
/tests
/deploy
/docs
```

---

# 59. Development Phases

# Phase 0 — Foundation and Architecture

Goal: Establish the product skeleton and core contracts.

Deliverables:

- Architecture
- Repo structure
- Workspace model
- Agent contract
- Skill contract
- Tool contract
- Model router contract
- Artifact model
- Event model
- Policy model
- Context2AI integration contract

Exit criteria:

- Core contracts reviewed
- Local development environment running
- CI/CD pipeline established
- Baseline identity, workspace authorization, read-only execution policy, audit events, approval contract, and secret handling are designed and reviewed before source data is connected.

---

# Phase 1 — MVP Autonomous Analytics

Goal: From selected source tables to validated analysis and Superset dashboard.

Scope for the first release: one explicitly selected workspace and source at a time, read-only source access, bounded query/data budgets, and human approval before any external publication. Cross-source federation, unattended publication, and source-system writes are outside this phase.

Agents:

- Supervisor
- Context
- Metadata
- Profiler
- Investigator
- SQL
- Data Scientist
- Visualization
- Superset Publisher
- REV Critic

Sources:

- PostgreSQL
- SQL Server
- CSV
- ServiceNow

Execution:

- SQL
- DuckDB
- Polars
- Python

Publishing:

- Superset

MVP success scenario:

> User selects Incident and Change Request tables and asks for operational analysis. System profiles, investigates, creates validated findings, creates dataset, builds Superset dashboard, and stores lineage.

---

# Phase 2 — Multi-Source and Semantic Analytics

Goal: Support cross-source analytics and reusable semantic models.

Add:

- Integration Agent
- Transformation Agent
- Semantic Model Agent
- Materialization
- Trino support
- Power BI adapter
- Existing-dashboard import
- Advanced metrics

---

# Phase 3 — Scheduled and Continuous Analytics

Goal: Move from one-time analytics to recurring intelligence.

Add:

- Scheduler
- Automated re-analysis
- Dashboard refresh
- Weekly/monthly summaries
- Alerting
- Metric drift
- Anomaly detection
- Data quality monitoring

---

# Phase 4 — Enterprise Governance and Scale

Goal: Enterprise production hardening.

Add:

- Advanced RBAC/ABAC
- Data policy enforcement
- Multi-tenancy
- Model governance
- Cost controls
- Audit dashboards
- Large-scale execution
- HA/DR

This phase strengthens baseline controls; it does not defer authentication, workspace isolation, server-side authorization, read-only data access, approval enforcement, or auditability until after the MVP. Do not describe an MVP or pilot as production-ready until the release gates in §70 are met.

---

# Phase 5 — Autonomous Discovery

Goal: Allow open-ended exploration.

Example:

> Find anything important in this dataset.

Capabilities:

- Autonomous hypothesis generation
- Broad exploration
- Prioritization
- Insight ranking
- Novel pattern detection
- Unsupervised analysis

---

# Phase 6 — Proactive Business Intelligence

Goal: Agents continuously detect and investigate meaningful business changes.

Capabilities:

- KPI monitoring
- Change-point detection
- Automatic investigation
- Automatic dashboard update
- Automatic report generation
- Policy-controlled proactive actions

---

# 60. Detailed Task Tracker

Legend:

- **P0** = Required for critical path
- **P1** = Important
- **P2** = Enhancement
- Status default = Not Started

## Phase 0 — Foundation

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| FND-001 | Create monorepo | P0 | — | Repo builds locally | Not Started |
| FND-002 | Define coding standards | P1 | FND-001 | Standards documented | Not Started |
| FND-003 | Create CI pipeline | P0 | FND-001 | PR build/test works | Not Started |
| FND-004 | Create CD pipeline skeleton | P1 | FND-003 | Dev deploy succeeds | Not Started |
| FND-005 | Define workspace schema | P0 | — | Schema reviewed | Not Started |
| FND-006 | Define agent contract | P0 | — | YAML/JSON schema complete | Not Started |
| FND-007 | Define skill contract | P0 | FND-006 | Schema complete | Not Started |
| FND-008 | Define tool contract | P0 | FND-006 | Schema complete | Not Started |
| FND-009 | Define artifact model | P0 | — | Artifact/version/lineage defined | Not Started |
| FND-010 | Define event model | P1 | — | Core event list finalized | Not Started |
| FND-011 | Define policy model | P0 | — | Execution policies represented | Not Started |
| FND-012 | Define model router contract | P0 | — | Provider abstraction complete | Not Started |
| FND-013 | Define logging standard | P1 | — | Correlation IDs supported | Not Started |
| FND-014 | Define error taxonomy | P1 | — | Retryable/non-retryable mapped | Not Started |
| FND-015 | Create local Docker environment | P0 | FND-001 | Full dev stack boots | Not Started |

## Phase 1A — Workspace and Platform Core

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| WSP-001 | Workspace create API | P0 | FND-005 | Create/read workspace works | Not Started |
| WSP-002 | Workspace update API | P0 | WSP-001 | Objective/config updates persist | Not Started |
| WSP-003 | Workspace member model | P1 | WSP-001 | Members/roles persist | Not Started |
| WSP-004 | Workspace source registration | P0 | WSP-001 | Sources attached to workspace | Not Started |
| WSP-005 | Workspace policy settings | P0 | FND-011 | Policies stored/enforced | Not Started |
| WSP-006 | Workspace artifact view | P1 | FND-009 | Artifacts listable | Not Started |
| WSP-007 | Workspace activity feed | P1 | FND-010 | Events visible chronologically | Not Started |
| WSP-008 | Workspace UI | P0 | WSP-001 | User can create/configure workspace | Not Started |

## Phase 1B — Context and Metadata

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| CTX-001 | Context2AI API client | P0 | FND-015 | Context retrieval works | Not Started |
| CTX-002 | Context semantic search adapter | P0 | CTX-001 | Query returns relevant context | Not Started |
| CTX-003 | Neo4j relationship query adapter | P1 | CTX-001 | Graph relationships returned | Not Started |
| CTX-004 | Business-term resolver | P1 | CTX-002 | Maps terms to technical fields | Not Started |
| CTX-005 | Context caching | P1 | CTX-001 | Repeat reads use cache | Not Started |
| META-001 | PostgreSQL metadata connector | P0 | WSP-004 | Schemas/tables/columns discovered | Not Started |
| META-002 | SQL Server metadata connector | P0 | WSP-004 | Metadata discovered | Not Started |
| META-003 | CSV metadata connector | P1 | WSP-004 | Schema inferred | Not Started |
| META-004 | ServiceNow metadata connector | P0 | WSP-004 | Table/field metadata discovered | Not Started |
| META-005 | Metadata normalization model | P0 | META-001 | Common metadata format | Not Started |
| META-006 | Source statistics collector | P1 | META-005 | Row count/freshness available | Not Started |

## Phase 1C — Tool and Model Runtime

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| TLR-001 | Tool registry service | P0 | FND-008 | Tools register/discover | Not Started |
| TLR-002 | Tool permission checks | P0 | TLR-001 | Unauthorized tool blocked | Not Started |
| TLR-003 | Tool execution audit | P0 | TLR-001 | Inputs/outputs/audit stored | Not Started |
| SKL-001 | Skill registry | P1 | FND-007 | Skills register/discover | Not Started |
| MOD-001 | Model router | P0 | FND-012 | Model profile routes requests | Not Started |
| MOD-002 | Provider fallback | P1 | MOD-001 | Fallback works on failure | Not Started |
| MOD-003 | Token/cost accounting | P1 | MOD-001 | Per-run cost stored | Not Started |
| MOD-004 | Prompt version registry | P1 | MOD-001 | Prompt version traceable | Not Started |

## Phase 1D — Query and Data Execution

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| QRY-001 | SQL execution gateway | P0 | META-001 | Controlled query execution works | Not Started |
| QRY-002 | Read-only SQL validation | P0 | QRY-001 | Writes rejected | Not Started |
| QRY-003 | Query timeout | P0 | QRY-001 | Long query cancelled | Not Started |
| QRY-004 | Query row limit | P0 | QRY-001 | Result limits enforced | Not Started |
| QRY-005 | Query audit | P0 | QRY-001 | Every query logged | Not Started |
| QRY-006 | Query cache | P1 | QRY-001 | Repeat query served from cache | Not Started |
| QRY-007 | SQL dialect abstraction | P1 | QRY-001 | PG + SQL Server supported | Not Started |
| DEX-001 | DuckDB execution engine | P0 | FND-015 | Local analytical SQL works | Not Started |
| DEX-002 | Polars dataframe engine | P0 | FND-015 | Dataframe analysis works | Not Started |
| DEX-003 | Python sandbox | P0 | FND-015 | Controlled Python execution works | Not Started |
| DEX-004 | Sandbox memory limits | P0 | DEX-003 | Memory cap enforced | Not Started |
| DEX-005 | Sandbox timeout | P0 | DEX-003 | Timeout enforced | Not Started |

## Phase 1E — Agent Runtime

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| AGT-001 | Agent runtime service | P0 | FND-006 | Agent definitions execute | Not Started |
| AGT-002 | Task state machine | P0 | AGT-001 | Full lifecycle persisted | Not Started |
| AGT-003 | Agent message persistence | P0 | AGT-001 | Messages stored | Not Started |
| AGT-004 | Supervisor agent | P0 | AGT-001 | Creates task plan | Not Started |
| AGT-005 | Context agent | P0 | CTX-001, AGT-001 | Retrieves business context | Not Started |
| AGT-006 | Metadata agent | P0 | META-005, AGT-001 | Retrieves technical metadata | Not Started |
| AGT-007 | Profiler agent | P0 | DEX-002 | Produces profile artifacts | Not Started |
| AGT-008 | Investigator agent | P0 | AGT-007 | Generates prioritized hypotheses | Not Started |
| AGT-009 | SQL agent | P0 | QRY-001 | Generates and executes valid SQL | Not Started |
| AGT-010 | Data Scientist agent | P0 | DEX-003 | Executes statistical analysis | Not Started |
| AGT-011 | Visualization agent | P1 | AGT-010 | Chart spec generated | Not Started |
| AGT-012 | REV critic agent | P0 | AGT-010 | Findings verified | Not Started |
| AGT-013 | Pause/resume | P1 | AGT-002 | Run pauses and resumes | Not Started |
| AGT-014 | User feedback injection | P1 | AGT-002 | Agent replans based on user input | Not Started |
| AGT-015 | Task retry policy | P0 | AGT-002 | Retryable failures recover | Not Started |

## Phase 1F — Profiling and Analysis

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| ANA-001 | Numeric profiling | P0 | AGT-007 | Numeric profile complete | Not Started |
| ANA-002 | Categorical profiling | P0 | AGT-007 | Category profile complete | Not Started |
| ANA-003 | Date/time profiling | P0 | AGT-007 | Coverage/granularity complete | Not Started |
| ANA-004 | Missingness analysis | P0 | AGT-007 | Null patterns identified | Not Started |
| ANA-005 | Duplicate detection | P1 | AGT-007 | Candidate duplicates detected | Not Started |
| ANA-006 | Correlation analysis | P1 | AGT-010 | Correlation results stored | Not Started |
| ANA-007 | Segmentation analysis | P0 | AGT-010 | Segment comparisons available | Not Started |
| ANA-008 | Pareto analysis | P1 | AGT-010 | 80/20 style analysis available | Not Started |
| ANA-009 | Trend analysis | P0 | AGT-010 | Time trends generated | Not Started |
| ANA-010 | Chi-square skill | P1 | AGT-010 | Valid statistical output | Not Started |
| ANA-011 | T-test skill | P1 | AGT-010 | Valid statistical output | Not Started |
| ANA-012 | ANOVA skill | P2 | AGT-010 | Valid statistical output | Not Started |
| ANA-013 | Logistic regression | P1 | AGT-010 | Driver analysis available | Not Started |
| ANA-014 | Feature importance | P1 | AGT-010 | Feature ranking available | Not Started |
| ANA-015 | Basic anomaly detection | P1 | AGT-010 | Outliers detected | Not Started |

## Phase 1G — Artifact and Insight Layer

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| ART-001 | Artifact registry service | P0 | FND-009 | Artifacts persist/version | Not Started |
| ART-002 | Artifact dependency model | P0 | ART-001 | Dependencies queryable | Not Started |
| ART-003 | Lineage service | P0 | ART-002 | End-to-end lineage available | Not Started |
| INS-001 | Insight schema | P0 | ART-001 | Findings stored as structured objects | Not Started |
| INS-002 | Evidence linking | P0 | INS-001 | Insight links to queries/results | Not Started |
| INS-003 | Verification status | P0 | AGT-012 | Verified state persisted | Not Started |
| INS-004 | Insight UI | P1 | INS-001 | User can inspect evidence | Not Started |

## Phase 1H — Superset Publishing

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| BI-001 | BI publisher interface | P0 | ART-001 | Generic interface defined | Not Started |
| BI-002 | Superset authentication adapter | P0 | BI-001 | Connection works | Not Started |
| BI-003 | Create dataset in Superset | P0 | BI-002 | Dataset publish works | Not Started |
| BI-004 | Create chart | P0 | BI-003 | Chart created programmatically | Not Started |
| BI-005 | Create dashboard | P0 | BI-004 | Dashboard created | Not Started |
| BI-006 | Apply filters | P1 | BI-005 | Filters configured | Not Started |
| BI-007 | Apply dashboard layout | P1 | BI-005 | Generated layout usable | Not Started |
| BI-008 | Store Superset external IDs | P0 | BI-005 | Artifact linked to Superset | Not Started |
| BI-009 | Update dashboard | P1 | BI-008 | Existing dashboard updated | Not Started |
| BI-010 | Publish approval gate | P0 | BI-005 | Approval required by policy | Not Started |

## Phase 1I — MVP UI

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| UI-001 | Workspace home | P0 | WSP-008 | Objective/sources visible | Not Started |
| UI-002 | Data explorer | P0 | META-005 | Tables/columns/profile visible | Not Started |
| UI-003 | Investigation tree | P0 | AGT-008 | Hypotheses/status visible | Not Started |
| UI-004 | Analysis canvas | P1 | ART-003 | Question-to-dashboard lineage visible | Not Started |
| UI-005 | Agent console | P0 | AGT-002 | Task/tool/model status visible | Not Started |
| UI-006 | Insight view | P0 | INS-004 | Findings/evidence visible | Not Started |
| UI-007 | Dashboard preview | P1 | BI-005 | Published output linked | Not Started |
| UI-008 | Human feedback input | P0 | AGT-014 | User can redirect agents | Not Started |

## Phase 2 — Multi-Source and Semantic Layer

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| INT-001 | Integration agent | P0 | AGT-001 | Cross-source plan generated | Not Started |
| INT-002 | Join-key discovery | P0 | META-005 | Candidate keys detected | Not Started |
| INT-003 | Join validation | P0 | INT-002 | Join cardinality verified | Not Started |
| INT-004 | Entity matching | P1 | INT-001 | Cross-source entity matching works | Not Started |
| INT-005 | Type normalization | P0 | INT-001 | Join types aligned | Not Started |
| TRN-001 | Transformation agent | P0 | INT-001 | Transform plans generated | Not Started |
| TRN-002 | Materialized dataset service | P0 | TRN-001 | Reusable dataset persisted | Not Started |
| TRN-003 | Incremental refresh | P1 | TRN-002 | Incremental load supported | Not Started |
| TRN-004 | Trino adapter | P1 | INT-001 | Federation supported | Not Started |
| SEM-001 | Semantic model service | P0 | ART-001 | Models stored/versioned | Not Started |
| SEM-002 | Metric definition | P0 | SEM-001 | KPI definitions persisted | Not Started |
| SEM-003 | Metric validation | P0 | SEM-002 | Metric tests supported | Not Started |
| SEM-004 | Dimension model | P1 | SEM-001 | Dimensions persisted | Not Started |
| SEM-005 | Metric duplication detection | P1 | SEM-002 | Duplicate/conflicting KPI flagged | Not Started |
| PBI-001 | Power BI adapter | P1 | BI-001 | Dataset/report integration works | Not Started |
| PBI-002 | Power BI publish workflow | P1 | PBI-001 | Artifact publish traceable | Not Started |
| BI-011 | Existing dashboard import | P1 | BI-001 | Existing dashboard metadata ingested | Not Started |
| BI-012 | Existing dashboard enhancement | P1 | BI-011 | Agent can modify/new-version | Not Started |

## Phase 3 — Scheduling and Continuous Analytics

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| SCH-001 | Scheduler service | P0 | FND-010 | Cron-like schedules work | Not Started |
| SCH-002 | Dataset refresh schedule | P0 | SCH-001 | Dataset refreshes automatically | Not Started |
| SCH-003 | Report schedule | P0 | SCH-001 | Report generation scheduled | Not Started |
| SCH-004 | Re-analysis schedule | P0 | SCH-001 | Analysis reruns automatically | Not Started |
| SCH-005 | Schedule run audit | P0 | SCH-001 | Every run recorded | Not Started |
| RPT-001 | Narrative report generator | P1 | INS-001 | Structured report produced | Not Started |
| RPT-002 | PDF export | P1 | RPT-001 | PDF output generated | Not Started |
| RPT-003 | Excel export | P1 | RPT-001 | Workbook output generated | Not Started |
| MON-001 | Metric monitor | P0 | SEM-002 | KPI thresholds monitored | Not Started |
| MON-002 | Drift detection | P1 | MON-001 | Significant drift detected | Not Started |
| MON-003 | Change-point detection | P1 | MON-001 | Regime change detected | Not Started |
| MON-004 | Data quality monitor | P1 | ANA-004 | DQ drift detected | Not Started |
| MON-005 | Automatic investigation trigger | P1 | MON-002 | New investigation created | Not Started |

## Phase 4 — Enterprise Hardening

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| SEC-001 | SSO integration | P0 | WSP-003 | Enterprise SSO works | Not Started |
| SEC-002 | RBAC | P0 | SEC-001 | Role checks enforced | Not Started |
| SEC-003 | ABAC | P1 | SEC-002 | Attribute policies enforced | Not Started |
| SEC-004 | Column-level policy | P0 | SEC-002 | Restricted columns blocked | Not Started |
| SEC-005 | Row-level policy | P1 | SEC-002 | Row policies enforced | Not Started |
| SEC-006 | PII masking | P0 | SEC-004 | Sensitive values masked | Not Started |
| SEC-007 | Cross-tenant isolation | P0 | SEC-002 | Tenant leakage tests pass | Not Started |
| GOV-001 | Governance agent | P0 | SEC-002 | Policy checks integrated | Not Started |
| GOV-002 | Publish destination controls | P0 | GOV-001 | Restricted publishing blocked | Not Started |
| GOV-003 | Model allowlist | P1 | MOD-001 | Only approved models used | Not Started |
| GOV-004 | Export policy | P0 | GOV-001 | Restricted exports blocked | Not Started |
| OPS-001 | HA deployment | P1 | Phase 1 | No single critical node | Not Started |
| OPS-002 | Disaster recovery | P1 | OPS-001 | Recovery procedure tested | Not Started |
| OPS-003 | Central observability dashboard | P0 | FND-013 | Platform metrics available | Not Started |
| OPS-004 | Cost dashboard | P1 | MOD-003 | LLM/query costs visible | Not Started |
| OPS-005 | Audit dashboard | P1 | GOV-001 | Security actions visible | Not Started |

## Phase 5 — Autonomous Discovery

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| AUT-001 | Broad discovery planner | P1 | AGT-004 | Open-ended analysis plans generated | Not Started |
| AUT-002 | Hypothesis ranking | P1 | AGT-008 | Hypotheses prioritized by value | Not Started |
| AUT-003 | Novelty scoring | P1 | ART-003 | Repeated findings deprioritized | Not Started |
| AUT-004 | Autonomous experiment loop | P1 | AGT-010 | Multiple analysis rounds run | Not Started |
| AUT-005 | Stop criteria | P0 | AUT-004 | Agent terminates safely | Not Started |
| AUT-006 | Discovery budget policy | P0 | MOD-003 | Autonomous cost controlled | Not Started |

## Phase 6 — Proactive Intelligence

| ID | Task | Priority | Depends On | Acceptance Criteria | Status |
|---|---|---:|---|---|---|
| PRO-001 | Proactive trigger engine | P1 | MON-005 | Meaningful events start analysis | Not Started |
| PRO-002 | Automatic root-cause flow | P1 | PRO-001 | Trigger launches investigation | Not Started |
| PRO-003 | Auto dashboard update | P1 | BI-009 | Dashboard updates under policy | Not Started |
| PRO-004 | Auto report update | P1 | RPT-001 | Report refreshes automatically | Not Started |
| PRO-005 | Executive notification workflow | P1 | PRO-002 | Validated summary delivered | Not Started |
| PRO-006 | Feedback learning | P2 | AGT-014 | User feedback influences future priority | Not Started |

---

# 61. MVP Critical Path

Recommended first critical path:

```text
Foundation
 ↓
Workspace
 ↓
Context2AI Adapter
 ↓
PostgreSQL + ServiceNow Metadata
 ↓
Query Gateway
 ↓
Polars / DuckDB / Python Sandbox
 ↓
Tool Registry
 ↓
Model Router
 ↓
Supervisor Agent
 ↓
Profiler Agent
 ↓
Investigator Agent
 ↓
SQL Agent
 ↓
Data Scientist Agent
 ↓
REV Agent
 ↓
Artifact Registry
 ↓
Insight Service
 ↓
Superset Publisher
 ↓
Workspace / Investigation UI
```

---

# 62. MVP Definition of Done

The MVP is complete when the following scenario works end to end:

1. User creates a workspace.
2. User adds a ServiceNow source.
3. User selects Incident and Change Request.
4. System loads Context2AI context.
5. System profiles the datasets.
6. System identifies data quality issues.
7. System proposes hypotheses.
8. System performs iterative analysis.
9. System produces at least three evidence-backed findings.
10. REV agent verifies findings.
11. System creates a reusable analytical dataset.
12. System creates KPI definitions.
13. System creates at least five charts.
14. System creates an executive dashboard.
15. System creates an operational dashboard.
16. System publishes to Superset.
17. All queries/artifacts/lineage are stored.
18. User can interrupt before publishing.
19. User can add a requirement and continue.
20. User can inspect the evidence behind every published KPI/finding.

### Safety and operational acceptance

The workflow above is accepted only when these checks also pass:

- Unauthorized tables and columns are rejected by the gateway across direct SQL, model-generated SQL, CTE/join references, repairs, fallbacks, and tool calls.
- A publication approval is bound to the exact plan and payload; editing or replanning after approval forces a new review.
- Pause, resume, cancellation, duplicate dispatch, worker restart, timeout, and partial-publication recovery have defined, observable outcomes.
- Representative analytical benchmarks validate joins, aggregates, metrics, statistical methods, and dashboard datasets against known expected results.
- Security checks cover prompt injection, scope escape, cross-workspace access, PII leakage, secret exposure, and unauthorized publication.
- The selected connector is exercised against a real supported source with least-privilege credentials, timeout/retry behavior, schema drift, and permission failures. Mock-only tests do not certify a connector.
- Deployment health, schema/migration parity, backup/restore, and capacity targets are verified in the environment being called a pilot or production environment.

---

# 63. Recommended MVP Demo Story

Use ServiceNow because it demonstrates the product clearly.

Demo question:

> Analyze Incident and Change data for the last 12 months. Identify the drivers of SLA breaches, recurring operational problems, teams with unusually high reassignment, and applications generating repeated critical incidents. Create an executive dashboard and an operations dashboard.

The demo should visibly show:

- Source discovery
- Context retrieval
- Profiling
- Hypothesis creation
- Agent task tree
- SQL
- Python/statistics
- Evidence
- REV verification
- KPI creation
- Dashboard design
- Superset publication
- Lineage
- Human intervention

---

# 64. Suggested Initial Team

Minimum implementation team:

- 1 Product / Architecture Lead
- 2 Backend Engineers
- 2 AI/Agent Engineers
- 1 Data Engineer
- 1 Data Scientist
- 1 Frontend Engineer
- 1 Platform/DevOps Engineer
- 1 QA / Automation Engineer

Optional:

- 1 BI engineer
- 1 Security engineer
- 1 UX designer

---

# 65. Key Risks

## Risk 1 — Too many agents

Mitigation:

- Keep 10–16 persistent roles.
- Implement most capabilities as skills.

## Risk 2 — Hallucinated analysis

Mitigation:

- Deterministic tools.
- Evidence requirements.
- REV verification.
- Reproducible queries.

## Risk 3 — Excessive LLM cost

Mitigation:

- Model routing.
- Caching.
- Cheap models for low-risk tasks.
- Token budgets.

## Risk 4 — Large data extraction

Mitigation:

- Source pushdown.
- Sampling.
- Query limits.
- Federation.

## Risk 5 — Unusable dashboards

Mitigation:

- Audience-based templates.
- BI design rules.
- Human approval.

## Risk 6 — Conflicting metrics

Mitigation:

- Semantic layer.
- Context2AI metric lookup.
- Metric validation.

## Risk 7 — Data leakage

Mitigation:

- Governance engine.
- Tool restrictions.
- Column-level controls.
- Sandbox isolation.

## Risk 8 — Agent loops never stop

Mitigation:

- Iteration limit.
- Budget.
- Confidence threshold.
- Stop criteria.
- Supervisor guardrails.

---

# 66. Future Extensions

Potential later capabilities:

- Natural-language report editing
- Agent-generated PowerPoint
- Executive meeting prep
- Forecast scenario modeling
- Prescriptive analytics
- Automated experimentation
- Digital twin integration
- Process mining
- Task mining
- Causal inference
- Optimization models
- Notebook generation
- Data contract generation
- dbt project generation
- Reverse ETL
- Business-action agents

---

# 67. Final Architecture Principle

The product should not be designed as:

```text
User
 ↓
LLM
 ↓
SQL
 ↓
Chart
```

It should be designed as:

```text
User / Business Objective
        ↓
Context2AI
        ↓
Supervisor
        ↓
Data Understanding
        ↓
Investigation
        ↓
Hypothesis
        ↓
Tool-Based Analysis
        ↓
Statistical Validation
        ↓
REV Verification
        ↓
Semantic Model
        ↓
Insight
        ↓
Dashboard / Report
        ↓
Publish / Schedule
        ↓
Continuous Monitoring
```

This is the key difference between an AI-assisted BI tool and an autonomous enterprise analytics operating system.

---

# 68. Product North Star

The long-term north star should be:

> A business user can select one or more governed enterprise datasets, describe a business problem, and receive a validated, explainable, reproducible analytical solution—including data preparation, statistical analysis, metrics, dashboards, reports, monitoring, and lineage—without manually coordinating multiple specialist teams.

---

# 69. Short Product Description

**Context2AI AnalystOS** is an autonomous analytics operating system that uses enterprise context, multi-agent reasoning, deterministic analytical tools, semantic modeling, and configurable BI publishing to automate the lifecycle from raw governed data to validated insights, dashboards, reports, and continuous business monitoring.

---

# 70. Implementation Status and Release Readiness

This specification defines intended behavior. Maintain implementation status in a dated, evidence-backed companion matrix; for each claim, record the code path, automated coverage, live-environment evidence, and remaining limitation. A passing unit suite alone does not establish source certification, production security, deployment parity, recovery, or scale.

| Readiness level | Minimum evidence |
|---|---|
| **Prototype** | Synthetic or explicitly approved non-sensitive data; isolated development environment; read-only access; visible limits and known unsupported paths. |
| **Controlled pilot** | Named owners and users; verified workspace access checks with positive and negative cases; each pilot connector tested against a real source; audit and approval paths exercised; operational monitoring and rollback/recovery documented. Clearly label unverified SSO, residency, connector, or scale capabilities. |
| **Production** | End-to-end SSO and role mapping; independent security review and remediation; secret rotation; live connector certification for every supported production connector; tested backup/restore and disaster recovery; deployment/schema parity; measured SLOs and load/capacity tests; incident and audit-export procedures; risk-tier evaluation thresholds met. |

Do not promote autonomy based solely on successful demos, benchmark averages, model consensus, or a populated evidence bundle. For consequential actions, evaluate false approvals on adversarial cases and retain a human approval gate until the applicable risk-tier threshold is demonstrated.

**Analysis references used for this revision:**

- **AIDataAnalyst:** [product, implementation, and deployment review (2026-09-16)](../AIDataAnalyst/Docs/review-2026-09-16/REVIEW.md) and [agent architecture critical review](../AIDataAnalyst/Docs/10-architecture/15-agent-architecture-critical-review.md). These informed the requirements that product/workspace scope reach the final SQL and tool execution boundary, and that implementation, configuration, deployment, and verification status be tracked separately.
- **AIDataEngineerAgentOS** (repository directory `AienginnerAgentOs`): [DataPilot architecture review (2026-09)](../AienginnerAgentOs/docs/ARCHITECTURE_REVIEW_2026-09.md) and [DataPilot implementation status matrix](../AienginnerAgentOs/docs/IMPLEMENTATION_STATUS_MATRIX.md). These informed the plan-bound approval, durable/idempotent workflow, fail-closed provider routing, control-plane isolation, connector certification, and release-evidence requirements.

Both are sibling projects. Their code, feature status, test results, and deployment claims are reference material only; they are not implementation or readiness evidence for Context2AI AnalystOS.
