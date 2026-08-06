-- Minimal auth.users stub so historical migrations that FK Supabase Auth
-- can apply on a disposable vanilla Postgres (GitHub Actions service).
-- Not used in production; Supabase already provides auth.users.

CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
    id UUID PRIMARY KEY
);
