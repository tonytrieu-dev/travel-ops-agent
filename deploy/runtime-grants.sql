REVOKE ALL ON ALL TABLES IN SCHEMA public FROM travel_runtime;
GRANT USAGE ON SCHEMA public TO travel_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO travel_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO travel_runtime;
REVOKE UPDATE, DELETE ON security_event, booking_transition, execution_event, compliance_assessment FROM travel_runtime;
REVOKE ALL ON alembic_version FROM travel_runtime;
