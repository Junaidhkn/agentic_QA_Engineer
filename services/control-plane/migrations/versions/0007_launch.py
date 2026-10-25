"""Launch readiness (Phase 11): legal acceptances and final plan metadata.

* ``legal_acceptances`` — which owner/admin accepted which version of the Terms, the
  Acceptable Use Policy (load-testing consent) or the DPA, when and from where. Append-only
  for the app role: a new version is a new row, nothing is edited.
* ``plans`` gains a display name and list price; limits are confirmed for launch.

Revision ID: 0007_launch
Revises: 0006_platform_checks
"""

from alembic import op

revision = "0007_launch"
down_revision = "0006_platform_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        CREATE TABLE legal_acceptances (
          id          uuid PRIMARY KEY,
          org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
          user_id     uuid NOT NULL REFERENCES users(id),
          document    text NOT NULL CHECK (document IN ('terms','aup','dpa')),
          version     text NOT NULL,
          accepted_at timestamptz NOT NULL DEFAULT now(),
          ip          inet,
          user_agent  text,
          UNIQUE (org_id, document, version)
        );
        ALTER TABLE legal_acceptances ENABLE ROW LEVEL SECURITY;
        ALTER TABLE legal_acceptances FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON legal_acceptances
          USING (org_id = ANY (app_org_ids())) WITH CHECK (org_id = ANY (app_org_ids()));
        GRANT SELECT, INSERT ON legal_acceptances TO re_app;
        GRANT SELECT, INSERT, DELETE ON legal_acceptances TO re_system;

        ALTER TABLE plans ADD COLUMN display_name text;
        ALTER TABLE plans ADD COLUMN price_usd_month integer;  -- NULL = contact sales
        ALTER TABLE plans ADD COLUMN sort_order integer NOT NULL DEFAULT 0;
        UPDATE plans SET display_name = 'Free', price_usd_month = 0, sort_order = 1 WHERE id = 'free';
        UPDATE plans SET display_name = 'Indie', price_usd_month = 49, sort_order = 2 WHERE id = 'indie';
        UPDATE plans SET display_name = 'Startup', price_usd_month = 299, sort_order = 3 WHERE id = 'startup';
        UPDATE plans SET display_name = 'Team', price_usd_month = NULL, sort_order = 4 WHERE id = 'team';
        ALTER TABLE plans ALTER COLUMN display_name SET NOT NULL;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DROP TABLE legal_acceptances;
        ALTER TABLE plans DROP COLUMN display_name, DROP COLUMN price_usd_month, DROP COLUMN sort_order;
        """
    )
