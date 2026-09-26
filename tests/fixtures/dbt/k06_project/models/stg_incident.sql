select number, priority, assignment_group, opened_at, resolved_at, made_sla, caller_email
from {{ source('servicenow', 'incident') }}
