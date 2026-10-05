-- Views over the files telemetry/receiver.py writes. `logs` is replaced by report.py with the logs folder.
-- Metrics are deltas: sum them. Skill names need OTEL_LOG_TOOL_DETAILS=1, which init sets.

CREATE OR REPLACE VIEW metrics AS
SELECT *, regexp_extract(filename, '([^/\\]+)[/\\]metrics\.jsonl$', 1) AS session_id
FROM read_json('{logs}/*/metrics.jsonl', format = 'newline_delimited', filename = true,
               columns = {ts: 'VARCHAR', name: 'VARCHAR', unit: 'VARCHAR', kind: 'VARCHAR', value: 'DOUBLE', attributes: 'JSON'});

CREATE OR REPLACE VIEW events AS
SELECT *, regexp_extract(filename, '([^/\\]+)[/\\]events\.jsonl$', 1) AS session_id
FROM read_json('{logs}/*/events.jsonl', format = 'newline_delimited', filename = true,
               columns = {ts: 'VARCHAR', event: 'VARCHAR', attributes: 'JSON'});

CREATE OR REPLACE VIEW cost_by_session AS
SELECT session_id, json_extract_string(attributes, '$.model') AS model, round(sum(value), 4) AS cost_usd
FROM metrics WHERE name = 'claude_code.cost.usage'
GROUP BY ALL ORDER BY session_id, cost_usd DESC;

CREATE OR REPLACE VIEW tokens_by_session AS
SELECT session_id, json_extract_string(attributes, '$.model') AS model, json_extract_string(attributes, '$.type') AS token_type, CAST(sum(value) AS BIGINT) AS tokens
FROM metrics WHERE name = 'claude_code.token.usage'
GROUP BY ALL ORDER BY session_id, model, token_type;

CREATE OR REPLACE VIEW tools_by_session AS
SELECT session_id, json_extract_string(attributes, '$.tool_name') AS tool, count(*) AS calls,
       count(*) FILTER (WHERE json_extract_string(attributes, '$.success') = 'true') AS succeeded,
       CAST(round(avg(TRY_CAST(json_extract_string(attributes, '$.duration_ms') AS DOUBLE))) AS BIGINT) AS avg_ms
FROM events WHERE event LIKE '%tool_result'
GROUP BY ALL ORDER BY session_id, calls DESC;

-- One row per skill call, however it was triggered: user-slash, claude-proactive or nested-skill.
CREATE OR REPLACE VIEW skill_calls AS
SELECT session_id, ts,
       json_extract_string(attributes, '$."skill.name"') AS skill,
       json_extract_string(attributes, '$.invocation_trigger') AS trigger,
       json_extract_string(attributes, '$."skill.source"') AS source
FROM events WHERE event LIKE '%skill_activated';

CREATE OR REPLACE VIEW skills_by_session AS
SELECT session_id, skill, trigger, count(*) AS calls, min(ts) AS first_call
FROM skill_calls GROUP BY ALL ORDER BY session_id, calls DESC;
