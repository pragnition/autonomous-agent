-- The admin user the agent signs in as through the dev impersonation switch. Safe to run again.
-- Prints the user's id: put it in localStorage under chilliclaw-dev-user-id.
WITH made AS (
  INSERT INTO tenants (name) SELECT 'default' WHERE NOT EXISTS (SELECT 1 FROM tenants) RETURNING id
), platform AS (
  SELECT id FROM made
  UNION ALL
  (SELECT id FROM tenants ORDER BY (slack_team_id IS NULL), created_at, id LIMIT 1)
  LIMIT 1
)
INSERT INTO users (tenant_id, display_name, first_name, email, role)
SELECT id, 'Agent Test Admin', 'Agent', 'agent-e2e@example.test', 'admin' FROM platform
ON CONFLICT (email) DO UPDATE SET role = 'admin'
RETURNING id;
