-- One-time app-scoped PostgreSQL role policy for roman_control_prod.
-- Run as a database superuser during application provisioning. This changes
-- privileges only; it does not create or alter application data or run Flyway.
DO $guard$
DECLARE
    current_user_is_superuser BOOLEAN;
BEGIN
    SELECT rolsuper INTO current_user_is_superuser
    FROM pg_roles
    WHERE rolname = current_user;

    IF current_database() <> 'roman_control_prod' THEN
        RAISE EXCEPTION 'Control Plane role policy must run only in roman_control_prod';
    END IF;
    IF current_user_is_superuser IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'Control Plane role policy requires a database superuser';
    END IF;
    IF (SELECT count(*) FROM pg_roles WHERE rolname IN (
        'roman_control_owner',
        'roman_control_migrator',
        'roman_control_runtime',
        'roman_control_backup',
        'roman_control_observer'
    )) <> 5 THEN
        RAISE EXCEPTION 'Control Plane database roles are incomplete';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_namespace n
        JOIN pg_roles r ON r.oid = n.nspowner
        WHERE n.nspname = 'roman_control'
          AND r.rolname = 'roman_control_owner'
    ) THEN
        RAISE EXCEPTION 'Control Plane schema is missing or has the wrong owner';
    END IF;
END
$guard$;

-- Keep the login used for read-only migration inspection from inheriting DDL
-- rights. The registered migration container uses SET ROLE only for migrate.
ALTER ROLE roman_control_migrator NOINHERIT;
GRANT roman_control_owner TO roman_control_migrator WITH INHERIT FALSE, SET TRUE;
GRANT CREATE ON DATABASE roman_control_prod TO roman_control_owner;

-- Remove cluster-wide data access from the application-scoped backup identity.
REVOKE pg_read_all_data FROM roman_control_backup;

-- Runtime and migration inspection can use only this application's schema.
GRANT USAGE ON SCHEMA roman_control
    TO roman_control_runtime, roman_control_backup, roman_control_migrator;
REVOKE ALL PRIVILEGES ON SCHEMA roman_control FROM roman_control_observer;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA roman_control
    TO roman_control_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA roman_control
    TO roman_control_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA roman_control
    TO roman_control_backup, roman_control_migrator;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA roman_control
    TO roman_control_backup, roman_control_migrator;
REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA roman_control FROM PUBLIC;
GRANT EXECUTE ON ALL ROUTINES IN SCHEMA roman_control TO roman_control_runtime;

-- Flyway creates its history table as roman_control_owner before applying V1.
-- Defaults therefore must exist before the first registered migration runs.
ALTER DEFAULT PRIVILEGES FOR ROLE roman_control_owner IN SCHEMA roman_control
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO roman_control_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE roman_control_owner IN SCHEMA roman_control
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO roman_control_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE roman_control_owner IN SCHEMA roman_control
    GRANT SELECT ON TABLES TO roman_control_backup, roman_control_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE roman_control_owner IN SCHEMA roman_control
    GRANT SELECT ON SEQUENCES TO roman_control_backup, roman_control_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE roman_control_owner
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE roman_control_owner IN SCHEMA roman_control
    GRANT EXECUTE ON FUNCTIONS TO roman_control_runtime;
