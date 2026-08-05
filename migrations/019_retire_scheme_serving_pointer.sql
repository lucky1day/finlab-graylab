-- Retire the unused legacy serving-pointer table.
-- Production execution MUST use scripts/apply_migrations.py so the canonical
-- runner can verify the exact source shape before this irreversible DDL.

DROP TABLE t_scheme_serving_pointer;
