"""Platform operations (operations §6, §7): metrics derived from the database, the synthetic
canary, integrity invariants and restore drills. Cross-tenant by nature, so it uses the system
role; it reads tenant tables only to count and verify, never to return tenant data."""
