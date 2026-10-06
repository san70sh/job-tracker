-- Adds Avature to the list of supported job systems. Safe to run more than once.
-- A fresh install gets this from db/schema.sql; an existing database needs it from here (scripts\db.ps1 apply runs it).
ALTER TYPE ats_type ADD VALUE IF NOT EXISTS 'avature' BEFORE 'icims';
