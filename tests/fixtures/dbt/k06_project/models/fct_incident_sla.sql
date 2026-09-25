select priority, assignment_group, count(*) as incidents, avg(case when made_sla then 1.0 else 0 end) as sla_rate
from {{ ref('stg_incident') }}
group by 1, 2
