-- 002: the shifts board and the assignment rules look shifts up by organisation and by worker.
CREATE INDEX shifts_by_org_start ON shifts (org_id, starts_at);
CREATE INDEX shifts_by_worker ON shifts (worker_id, starts_at);
