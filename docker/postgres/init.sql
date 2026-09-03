-- Runs once, on first initialisation of the data volume.
-- Creates the pgvector extension in the main database and provisions the
-- throwaway database the test suite points at.
CREATE EXTENSION IF NOT EXISTS vector;

SELECT 'CREATE DATABASE offers_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'offers_test')\gexec

\connect offers_test
CREATE EXTENSION IF NOT EXISTS vector;
