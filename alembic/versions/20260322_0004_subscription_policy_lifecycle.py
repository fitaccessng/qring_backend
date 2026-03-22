"""subscription policy lifecycle

Revision ID: 20260322_0004
Revises: 20260306_0003
Create Date: 2026-03-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


# revision identifiers, used by Alembic.
revision = "20260322_0004"
down_revision = "20260306_0003"
branch_labels = None
depends_on = None


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    if "subscriptions" in table_names:
        columns = {col["name"]: col for col in inspector.get_columns("subscriptions")}

        def add_column(name: str, column):
            if name not in columns:
                op.add_column("subscriptions", column)

        add_column("tenant_type", sa.Column("tenant_type", sa.String(length=20), nullable=False, server_default="homeowner"))
        add_column("tenant_id", sa.Column("tenant_id", sa.String(length=36), nullable=True))
        add_column("billing_scope", sa.Column("billing_scope", sa.String(length=20), nullable=False, server_default="homeowner"))
        add_column("auto_renew", sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.text("true")))
        add_column("cancel_at_period_end", sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.text("false")))
        add_column("grace_days", sa.Column("grace_days", sa.Integer(), nullable=False, server_default="5"))
        add_column("grace_ends_at", sa.Column("grace_ends_at", sa.DateTime(), nullable=True))
        add_column("warning_phase", sa.Column("warning_phase", sa.String(length=20), nullable=True))
        add_column("suspension_reason", sa.Column("suspension_reason", sa.String(length=50), nullable=True))
        add_column("last_payment_attempt_at", sa.Column("last_payment_attempt_at", sa.DateTime(), nullable=True))
        add_column("last_successful_payment_at", sa.Column("last_successful_payment_at", sa.DateTime(), nullable=True))
        add_column("amount_due", sa.Column("amount_due", sa.Numeric(12, 2), nullable=False, server_default="0"))
        add_column("amount_paid", sa.Column("amount_paid", sa.Numeric(12, 2), nullable=False, server_default="0"))
        add_column("timezone", sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Africa/Lagos"))

        inspector = inspect(bind)
        if not _has_index(inspector, "subscriptions", "ix_subscriptions_tenant_id"):
            op.execute(text("CREATE INDEX IF NOT EXISTS ix_subscriptions_tenant_id ON subscriptions (tenant_id)"))

        if bind.dialect.name == "sqlite":
            op.execute(
                text(
                    """
                    UPDATE subscriptions
                    SET tenant_type = COALESCE(NULLIF(tenant_type, ''), 'homeowner'),
                        tenant_id = COALESCE(tenant_id, user_id),
                        billing_scope = COALESCE(NULLIF(billing_scope, ''), 'homeowner'),
                        grace_days = COALESCE(grace_days, 5),
                        timezone = COALESCE(NULLIF(timezone, ''), 'Africa/Lagos'),
                        grace_ends_at = COALESCE(
                            grace_ends_at,
                            CASE
                                WHEN ends_at IS NOT NULL THEN datetime(ends_at, '+5 day')
                                WHEN trial_ends_at IS NOT NULL THEN datetime(trial_ends_at, '+5 day')
                                ELSE NULL
                            END
                        )
                    """
                )
            )
        else:
            op.execute(
                text(
                    """
                    UPDATE subscriptions
                    SET tenant_type = COALESCE(NULLIF(tenant_type, ''), 'homeowner'),
                        tenant_id = COALESCE(tenant_id, user_id),
                        billing_scope = COALESCE(NULLIF(billing_scope, ''), 'homeowner'),
                        grace_days = COALESCE(grace_days, 5),
                        timezone = COALESCE(NULLIF(timezone, ''), 'Africa/Lagos'),
                        grace_ends_at = COALESCE(
                            grace_ends_at,
                            CASE
                                WHEN ends_at IS NOT NULL THEN ends_at + INTERVAL '5 day'
                                WHEN trial_ends_at IS NOT NULL THEN trial_ends_at + INTERVAL '5 day'
                                ELSE NULL
                            END
                        )
                    """
                )
            )

    if "subscription_invoices" not in table_names:
        op.create_table(
            "subscription_invoices",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("subscription_id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=20), nullable=False, server_default="paystack"),
            sa.Column("provider_reference", sa.String(length=120), nullable=True),
            sa.Column("amount_expected", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("amount_received", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("currency", sa.String(length=10), nullable=False, server_default="NGN"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("due_at", sa.DateTime(), nullable=True),
            sa.Column("paid_at", sa.DateTime(), nullable=True),
            sa.Column("raw_payload", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], name=op.f("fk_subscription_invoices_subscription_id_subscriptions")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_invoices")),
        )
        op.execute(text("CREATE INDEX IF NOT EXISTS ix_subscription_invoices_provider_reference ON subscription_invoices (provider_reference)"))
        op.execute(text("CREATE INDEX IF NOT EXISTS ix_subscription_invoices_subscription_id ON subscription_invoices (subscription_id)"))

    if "payment_attempts" not in table_names:
        op.create_table(
            "payment_attempts",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("subscription_id", sa.String(length=36), nullable=False),
            sa.Column("invoice_id", sa.String(length=36), nullable=True),
            sa.Column("provider", sa.String(length=20), nullable=False, server_default="paystack"),
            sa.Column("provider_reference", sa.String(length=120), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("failure_code", sa.String(length=50), nullable=True),
            sa.Column("failure_reason", sa.Text(), nullable=True),
            sa.Column("attempted_at", sa.DateTime(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["invoice_id"], ["subscription_invoices.id"], name=op.f("fk_payment_attempts_invoice_id_subscription_invoices")),
            sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], name=op.f("fk_payment_attempts_subscription_id_subscriptions")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_attempts")),
        )
        op.execute(text("CREATE INDEX IF NOT EXISTS ix_payment_attempts_provider_reference ON payment_attempts (provider_reference)"))
        op.execute(text("CREATE INDEX IF NOT EXISTS ix_payment_attempts_subscription_id ON payment_attempts (subscription_id)"))

    if "subscription_events" not in table_names:
        op.create_table(
            "subscription_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("subscription_id", sa.String(length=36), nullable=False),
            sa.Column("event_type", sa.String(length=50), nullable=False),
            sa.Column("old_status", sa.String(length=30), nullable=True),
            sa.Column("new_status", sa.String(length=30), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], name=op.f("fk_subscription_events_subscription_id_subscriptions")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_events")),
        )
        op.execute(text("CREATE INDEX IF NOT EXISTS ix_subscription_events_event_type ON subscription_events (event_type)"))
        op.execute(text("CREATE INDEX IF NOT EXISTS ix_subscription_events_subscription_id ON subscription_events (subscription_id)"))

    if "subscription_notifications" not in table_names:
        op.create_table(
            "subscription_notifications",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("subscription_id", sa.String(length=36), nullable=False),
            sa.Column("channel", sa.String(length=20), nullable=False),
            sa.Column("template_key", sa.String(length=80), nullable=False),
            sa.Column("warning_phase", sa.String(length=20), nullable=True),
            sa.Column("scheduled_for", sa.DateTime(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("delivery_status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("dedupe_key", sa.String(length=120), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], name=op.f("fk_subscription_notifications_subscription_id_subscriptions")),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_notifications")),
        )
        op.execute(text("CREATE INDEX IF NOT EXISTS ix_subscription_notifications_dedupe_key ON subscription_notifications (dedupe_key)"))
        op.execute(text("CREATE INDEX IF NOT EXISTS ix_subscription_notifications_subscription_id ON subscription_notifications (subscription_id)"))


def downgrade() -> None:
    # Non-destructive downgrade.
    pass
