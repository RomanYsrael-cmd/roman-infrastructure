CREATE TABLE migration_integration (
    id integer PRIMARY KEY,
    release_name text NOT NULL
);

INSERT INTO migration_integration (id, release_name)
VALUES (1, 'multi-release');
