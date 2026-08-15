# P9 Decision: Multi-Region Deployment

**Status:** SKIP (deferred)  
**Date:** 2025-01-15  
**Owner:** Lead

---

## Decision

Do not implement multi-region CockroachDB deployment in Phase 9.

## Rationale

Multi-region is a CockroachDB operational capability, not a code change. The PQBS schema and application layer are already multi-region-compatible: CockroachDB's multi-region abstractions (regional tables, global tables, survival goals) operate at the schema level and require no application code changes beyond the initial migration.

Implementing multi-region for the hackathon submission would require:
1. A paid CockroachDB Dedicated cluster (multi-region is not available on Serverless free tier).
2. Schema migrations to add `REGION` column and `REGIONAL BY ROW` table definitions.
3. Operational changes to DNS, load balancing, and IAM.

None of these demonstrate novel application logic. They would consume budget and time without adding to the evidence base for the hackathon evaluation criteria.

## If Implemented in Production

The correct approach would be:
- Mark `belief` and `working_memory` as `REGIONAL BY ROW` (low-latency reads for tenant's home region).
- Mark `quarantine` and `audit_event` as `GLOBAL` (consistency-critical, cross-region reads acceptable).
- Set `SURVIVE REGION FAILURE` on the database for HA guarantee.
- Deploy the application layer in each region with region-affinity routing.

The application code (ingest, gate, recall) is already written to be stateless and connection-agnostic — it would work without modification in a multi-region deployment.

## Deferral Conditions

Implement when: moving to a production deployment with SLA requirements for regional availability.
