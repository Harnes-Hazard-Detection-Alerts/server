-- Product flow: one operator works on one assigned excavator at a time.
-- Existing duplicate active rows intentionally make this migration fail and
-- roll back so the team can resolve them without silently losing audit data.
CREATE UNIQUE INDEX assignments_one_active_per_operator
    ON assignments (operator_id) WHERE status = 'active';
