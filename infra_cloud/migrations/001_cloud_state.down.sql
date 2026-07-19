-- Migration 001 down: Remove cloud state tables

DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS subscriptions;
DROP TABLE IF EXISTS deployments;
DROP TABLE IF EXISTS customers;
