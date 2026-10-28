"""Remember explicit native project membership without inferring old history.

Revision ID: v2_45
Revises: v2_44
"""

import sqlalchemy as sa
from alembic import op

revision = "v2_45"
down_revision = "v2_44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sessions")}
    if "native_project_assigned" not in columns:
        op.add_column("sessions", sa.Column("native_project_assigned", sa.Integer()))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sessions")}
    if "native_project_assigned" in columns:
        op.drop_column("sessions", "native_project_assigned")
